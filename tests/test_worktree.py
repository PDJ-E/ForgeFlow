from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from forgeflow.errors import WorktreeError
from forgeflow.jobs import JobState
from forgeflow.worktree import WorktreeManager, parse_worktree_list, prepare_job_worktree
from tests.support import GitRepoTestCase, run_git


class WorktreeLifecycleTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = WorktreeManager(self.repo)
        # Unrelated, uncommitted user work in the main checkout must survive everything.
        (self.repo / "README.md").write_text("# demo\n\nuser edit in progress\n", encoding="utf-8")
        (self.repo / "scratch.txt").write_text("untracked user notes\n", encoding="utf-8")
        self.main_head = run_git(self.repo, "rev-parse", "HEAD")

    def assert_main_untouched(self) -> None:
        self.assertEqual(run_git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(run_git(self.repo, "rev-parse", "HEAD"), self.main_head)
        self.assertEqual(
            run_git(self.repo, "status", "--porcelain").splitlines(),
            [" M README.md", "?? scratch.txt"],
        )
        self.assertIn("user edit in progress", (self.repo / "README.md").read_text("utf-8"))

    def test_creates_isolated_worktree_from_origin_main(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        wt = Path(job.worktree.path)
        self.assertEqual(job.worktree.branch, f"forge/{job.job_id}")
        self.assertEqual(job.worktree.base_ref, "origin/main")
        self.assertEqual(job.worktree.base_sha, self.main_head)
        self.assertEqual(run_git(wt, "branch", "--show-current"), f"forge/{job.job_id}")
        self.assertEqual(run_git(wt, "status", "--porcelain"), "")
        # No upstream is configured, so a bare `git push` cannot target main by accident.
        upstream = subprocess.run(
            ["git", "-C", str(wt), "config", "--get", f"branch.forge/{job.job_id}.merge"],
            capture_output=True,
        )
        self.assertNotEqual(upstream.returncode, 0)
        self.assert_main_untouched()

    def test_refuses_before_plan_approval(self) -> None:
        job = self.new_job()
        with self.assertRaisesRegex(WorktreeError, "after plan approval"):
            prepare_job_worktree(self.store, self.manager, job.job_id)
        self.assertFalse(self.manager.path_for(job.job_id).exists())

    def test_reuses_existing_worktree_and_keeps_dirty_work(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        wip = Path(job.worktree.path) / "wip.txt"
        wip.write_text("half-done\n", encoding="utf-8")
        again = prepare_job_worktree(self.store, self.manager, job.job_id)
        self.assertEqual(again.worktree, job.worktree)
        self.assertEqual(wip.read_text("utf-8"), "half-done\n")
        self.assertTrue(self.manager.inspect(Path(job.worktree.path)).dirty)
        self.assert_main_untouched()

    def test_refuses_unknown_directory_at_target_path(self) -> None:
        job = self.approved_job()
        target = self.manager.path_for(job.job_id)
        target.mkdir(parents=True)
        (target / "someone-elses.txt").write_text("keep me\n", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "not empty"):
            prepare_job_worktree(self.store, self.manager, job.job_id)
        self.assertEqual((target / "someone-elses.txt").read_text("utf-8"), "keep me\n")

    def test_refuses_preexisting_branch(self) -> None:
        job = self.approved_job()
        run_git(self.repo, "branch", f"forge/{job.job_id}")
        with self.assertRaisesRegex(WorktreeError, "already exists"):
            prepare_job_worktree(self.store, self.manager, job.job_id)

    def test_refuses_unrecorded_registered_worktree(self) -> None:
        job = self.approved_job()
        target = self.manager.path_for(job.job_id)
        run_git(self.repo, "worktree", "add", "-q", "-b", "user/branch", str(target))
        with self.assertRaisesRegex(WorktreeError, "not recorded"):
            prepare_job_worktree(self.store, self.manager, job.job_id)

    def test_reattaches_after_directory_was_removed_cleanly(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        wt = Path(job.worktree.path)
        (wt / "feature.txt").write_text("committed work\n", encoding="utf-8")
        run_git(wt, "add", "feature.txt")
        run_git(wt, "commit", "-q", "-m", "work")
        self.manager.remove(job.worktree)
        self.assertFalse(wt.exists())
        prepare_job_worktree(self.store, self.manager, job.job_id)
        self.assertEqual((wt / "feature.txt").read_text("utf-8"), "committed work\n")

    def test_missing_directory_still_registered_is_left_for_a_human(self) -> None:
        from tests.support import rmtree

        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        rmtree(Path(job.worktree.path))
        with self.assertRaisesRegex(WorktreeError, "prunable"):
            prepare_job_worktree(self.store, self.manager, job.job_id)

    def test_remove_refuses_dirty_worktree(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        (Path(job.worktree.path) / "unsaved.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "uncommitted"):
            self.manager.remove(job.worktree)
        self.assertTrue((Path(job.worktree.path) / "unsaved.txt").exists())

    def test_remove_keeps_the_branch(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        assert job.worktree is not None
        self.manager.remove(job.worktree)
        self.assertEqual(
            run_git(self.repo, "branch", "--list", job.worktree.branch).strip(), job.worktree.branch
        )
        self.assert_main_untouched()

    def test_worktrees_do_not_show_up_in_main_status(self) -> None:
        prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        self.assert_main_untouched()

    def test_cancelled_job_cannot_revisit_worktree(self) -> None:
        job = prepare_job_worktree(self.store, self.manager, self.approved_job().job_id)
        self.store.transition(job.job_id, JobState.CANCELLED, reason="dropped")
        with self.assertRaises(WorktreeError):
            prepare_job_worktree(self.store, self.manager, job.job_id)


class PathFormTests(GitRepoTestCase):
    def test_short_8dot3_repo_path_is_the_same_repository(self) -> None:
        # GitHub's Windows runners keep %TEMP% under an 8.3 name (RUNNER~1); git reports long paths.
        if sys.platform != "win32":
            self.skipTest("8.3 short names are a Windows filesystem feature")
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(self.repo), buf, 1024)
        if not buf.value or buf.value == str(self.repo):
            self.skipTest("8.3 name generation is disabled on this volume")
        short = Path(buf.value)
        job = prepare_job_worktree(self.store, WorktreeManager(short), self.approved_job().job_id)
        assert job.worktree is not None
        self.assertTrue(Path(job.worktree.path).is_dir())
        # Re-entry through the other path form must recognise the same worktree.
        prepare_job_worktree(self.store, WorktreeManager(self.repo), job.job_id)


class ParseWorktreeListTests(unittest.TestCase):
    def test_parses_porcelain(self) -> None:
        out = (
            "worktree C:/r\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree C:/r/.forge/worktrees/x\nHEAD def\ndetached\nprunable gitdir file\n"
        )
        main, other = parse_worktree_list(out)
        self.assertEqual((main.path, main.branch), ("C:/r", "refs/heads/main"))
        self.assertTrue(other.detached and other.prunable)


if __name__ == "__main__":
    unittest.main()
