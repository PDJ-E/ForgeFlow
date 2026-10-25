"""Regression tests for the eight defects found in the independent review of PR #3.

Each class names the finding it covers. The tests marked "exploit" use only APIs that existed
before the fix and were run against the unfixed code to confirm that they failed there.

MOCKED: GitHub responses (FakeGitHub) and MockProvider. REAL: git repositories, worktrees,
child processes, OS locks and the check commands run by ``run_checks``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from forgeflow.dispatch import run_builder
from forgeflow.errors import (
    InvalidTransitionError,
    LeaseError,
    ProviderError,
    ValidationError,
    WorktreeError,
)
from forgeflow.github import GitHubClient
from forgeflow.jobs import JobState, Worktree
from forgeflow.proc import kill_process_tree, pid_alive, run_command
from forgeflow.providers import MockProvider, ProviderTask, build_task
from forgeflow.worktree import WorktreeManager, prepare_job_worktree
from tests.support import FakeGitHub, GitRepoTestCase, pull_request, rmtree, run_git

S = JobState
CRITERIA = ("greet() returns 'hello'",)
PASSING_CHECK = ((sys.executable, "-c", "pass"),)


def _sleeper(seconds: int = 60) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _wait_dead(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


# --- 1. worktree redirection -------------------------------------------------------------------


class WorktreeRedirectionTests(GitRepoTestCase):
    def builder_task(self, job_id: str) -> ProviderTask:
        return build_task(
            self.store,
            job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )

    def test_exploit_update_cannot_redirect_the_worktree_to_main(self) -> None:
        job = self.ready_job()

        def redirect(j: Any) -> None:
            assert j.worktree is not None
            j.worktree = Worktree(
                path=str(self.repo),
                branch=j.worktree.branch,
                base_ref=j.worktree.base_ref,
                base_sha=j.worktree.base_sha,
                created_by_forgeflow=True,
            )

        with self.assertRaises(InvalidTransitionError):
            self.store.update(job.job_id, redirect, event="redirect")
        with self.assertRaises(InvalidTransitionError):
            self.store.update(
                job.job_id, lambda j: setattr(j.repo, "root", str(self.tmp)), event="x"
            )

    def test_dispatch_refuses_a_tampered_record_pointing_at_main(self) -> None:
        job = self.ready_job()
        task = self.builder_task(job.job_id)
        self.tamper(job.job_id, lambda d: d["worktree"].update(path=str(self.repo)))
        tampered = replace(task, worktree_path=str(self.repo))
        builder = MockProvider("mock", writes={"src/evil.py": "x = 1\n"})
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                run_builder(self.store, lease, builder, tampered)
        self.assertFalse((self.repo / "src").exists(), "builder output reached the main checkout")
        self.assertEqual(builder.executed, [])

    def test_dispatch_refuses_another_registered_worktree(self) -> None:
        job = self.ready_job()
        task = self.builder_task(job.job_id)
        other = self.tmp / "other-worktree"
        run_git(self.repo, "worktree", "add", "-q", "-b", f"forge/{job.job_id}-x", str(other))
        self.tamper(job.job_id, lambda d: d["worktree"].update(path=str(other)))
        tampered = replace(task, worktree_path=str(other))
        builder = MockProvider("mock", writes={"src/evil.py": "x = 1\n"})
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                run_builder(self.store, lease, builder, tampered)
        self.assertFalse((other / "src").exists())

    def test_store_refuses_a_job_for_another_repository(self) -> None:
        record = self.repo_record()
        record.root = str(self.tmp)
        with self.assertRaises(ValidationError):
            self.store.create("elsewhere", record, self.new_job().policy)


# --- 2. orphaned providers ---------------------------------------------------------------------


class OrphanedProviderTests(GitRepoTestCase):
    def building_with_provider(self) -> tuple[str, Any, subprocess.Popen[bytes]]:
        job = self.ready_job()
        provider = _sleeper()
        self.addCleanup(provider.wait, timeout=10)
        self.addCleanup(provider.kill)
        lease = self.store.acquire_lease(job.job_id, "builder", "mock")
        self.addCleanup(lease._os_lock.release)
        self.store.transition(job.job_id, S.BUILDING, lease_token=lease.token)
        lease.set_worker_pid(provider.pid)
        return job.job_id, lease, provider

    def test_exploit_release_does_not_free_a_job_whose_provider_still_runs(self) -> None:
        job_id, lease, provider = self.building_with_provider()
        lease.release()
        job = self.store.load(job_id)
        self.assertEqual(job.state, S.BUILDING)
        self.assertIsNotNone(job.lease, "the lease record must survive while the provider runs")
        with self.assertRaises(LeaseError):
            self.store.acquire_lease(job_id, "builder", "mock")
        self.assertEqual(self.store.recover(job_id).outcome, "orphaned-worker")

        provider.kill()
        provider.wait(timeout=10)
        self.assertTrue(_wait_dead(provider.pid))
        self.assertEqual(self.store.recover(job_id).outcome, "recovered")
        self.store.acquire_lease(job_id, "builder", "mock").release()

    def test_exploit_escalation_cannot_drop_a_live_provider(self) -> None:
        job_id, lease, _ = self.building_with_provider()
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job_id, S.NEEDS_HUMAN, lease_token=lease.token, reason="stuck")
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job_id, S.VERIFYING, lease_token=lease.token)
        self.assertEqual(self.store.load(job_id).state, S.BUILDING)

    def test_exploit_force_release_refuses_a_verified_live_provider(self) -> None:
        job_id, lease, _ = self.building_with_provider()
        lease._os_lock.release()  # the orchestrator dies; its provider keeps running
        with self.assertRaises(LeaseError):
            self.store.force_release_lease(job_id, reason="looks stuck")
        self.assertIsNotNone(self.store.load(job_id).lease)

    def test_a_reused_pid_is_not_mistaken_for_the_provider(self) -> None:
        job_id, lease, _ = self.building_with_provider()
        lease._os_lock.release()
        # Same PID, different start time: the recorded provider is gone and the PID was reused.
        self.tamper(job_id, lambda d: d["lease"].update(worker_started_at=1))
        report = self.store.recover(job_id)
        self.assertEqual(report.outcome, "recovered", report.detail)


# --- 3. ignored-file deletion -----------------------------------------------------------------


class IgnoredFileCleanupTests(GitRepoTestCase):
    def test_exploit_remove_refuses_a_worktree_with_ignored_files(self) -> None:
        manager = WorktreeManager(self.repo)
        job = prepare_job_worktree(self.store, manager, self.approved_job().job_id)
        assert job.worktree is not None
        wt = Path(job.worktree.path)
        (wt / ".gitignore").write_text(".env\nlocal-data/\n", encoding="utf-8")
        run_git(wt, "add", ".gitignore")
        run_git(wt, "commit", "-q", "-m", "ignore local files")
        (wt / ".env").write_text("API_TOKEN=do-not-lose\n", encoding="utf-8")
        (wt / "local-data").mkdir()
        (wt / "local-data" / "notes.db").write_text("user data\n", encoding="utf-8")
        self.assertFalse(manager.inspect(wt).dirty, "ignored files are not 'dirty' to git")

        with self.assertRaisesRegex(WorktreeError, "ignored"):
            manager.remove(job.worktree)
        self.assertEqual((wt / ".env").read_text("utf-8"), "API_TOKEN=do-not-lose\n")
        self.assertTrue((wt / "local-data" / "notes.db").exists())


# --- 4. invalid delivery evidence ---------------------------------------------------------------


class DeliveryEvidenceTests(GitRepoTestCase):
    def commit_work(self, job_id: str, content: str) -> str:
        job = self.store.load(job_id)
        assert job.worktree is not None
        wt = Path(job.worktree.path)
        (wt / "src").mkdir(exist_ok=True)
        (wt / "src" / "greet.py").write_text(content, encoding="utf-8")
        run_git(wt, "add", "-A")
        run_git(wt, "commit", "-q", "-m", "work")
        return run_git(wt, "rev-parse", "HEAD")

    def github(self, job_id: str, head_sha: str, **pr: Any) -> GitHubClient:
        fake = FakeGitHub(
            {},
            pull_requests={
                42: pull_request(42, head_ref=f"forge/{job_id}", head_sha=head_sha, **pr)
            },
        )
        return GitHubClient(runner=fake)

    def reviewing_with_real_checks(self, job_id: str, token: str) -> str:
        """Build by hand (no provider), commit, run real checks and reach REVIEWING."""
        self.store.transition(job_id, S.BUILDING, lease_token=token)
        head = self.commit_work(job_id, "def greet():\n    return 'hello'\n")
        self.store.transition(job_id, S.VERIFYING, lease_token=token)
        evidence = self.store.run_checks(job_id, token, PASSING_CHECK)
        self.assertEqual(
            (evidence.status, evidence.mocked, evidence.commit), ("passed", False, head)
        )
        self.store.transition(job_id, S.REVIEWING, lease_token=token)
        self.link_draft_pr(job_id)
        return head

    def test_exploit_mocked_or_claimed_checks_cannot_open_gate_b(self) -> None:
        job = self.ready_job()
        self.link_draft_pr(job.job_id)
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
            for mocked in (True, False):  # a simulated result, and an unverified claim
                try:
                    self.store.record_evidence(job.job_id, "checks", "passed", {}, mocked=mocked)
                except ValidationError:
                    pass  # fixed: "checks" evidence can only come from run_checks()
            self.store.transition(job.job_id, S.REVIEWING, lease_token=t)
            with self.assertRaises(InvalidTransitionError):
                self.store.transition(job.job_id, S.AWAITING_MERGE_APPROVAL, lease_token=t)
            self.store.transition(job.job_id, S.NEEDS_HUMAN, lease_token=t, reason="done")

    def test_exploit_merge_is_not_recorded_from_a_caller_claim(self) -> None:
        job = self.ready_job()
        self.link_draft_pr(job.job_id)
        self.tamper(job.job_id, lambda d: d.update(state="awaiting_merge_approval"))
        claim = {"source": "github", "pr_number": 42, "merged_at": "2026-10-25T00:00:00Z"}
        # The store no longer takes a merge claim at all (TypeError) or refuses it.
        with self.assertRaises((InvalidTransitionError, TypeError)):
            cast(Any, self.store).transition(job.job_id, S.MERGED, merge_observation=claim)
        self.assertEqual(self.store.load(job.job_id).state, S.AWAITING_MERGE_APPROVAL)

    def test_verified_checks_and_pr_open_gate_b_and_record_the_merge(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            head = self.reviewing_with_real_checks(job.job_id, lease.token)
            # The draft PR must carry exactly the validated commit.
            with self.assertRaisesRegex(InvalidTransitionError, "commit"):
                self.store.request_merge_approval(
                    job.job_id, lease.token, self.github(job.job_id, "0" * 40)
                )
            with self.assertRaisesRegex(InvalidTransitionError, "draft"):
                self.store.request_merge_approval(
                    job.job_id, lease.token, self.github(job.job_id, head, draft=False)
                )
            self.store.request_merge_approval(
                job.job_id, lease.token, self.github(job.job_id, head)
            )
        self.assertEqual(self.store.load(job.job_id).state, S.AWAITING_MERGE_APPROVAL)

        with self.assertRaisesRegex(InvalidTransitionError, "not merged"):
            self.store.record_merge(job.job_id, self.github(job.job_id, head))
        with self.assertRaisesRegex(InvalidTransitionError, "commit"):
            self.store.record_merge(job.job_id, self.github(job.job_id, "f" * 40, merged=True))
        merged = self.store.record_merge(job.job_id, self.github(job.job_id, head, merged=True))
        self.assertEqual(merged.state, S.MERGED)

    def test_stale_checks_from_an_earlier_round_are_rejected(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            self.reviewing_with_real_checks(job.job_id, t)
            self.store.transition(job.job_id, S.BUILDING, lease_token=t)  # rework round
            head = self.commit_work(job.job_id, "def greet():\n    return 'hi'\n")
            self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
            self.store.transition(job.job_id, S.REVIEWING, lease_token=t)  # no new checks
            with self.assertRaisesRegex(InvalidTransitionError, "current build round"):
                self.store.request_merge_approval(job.job_id, t, self.github(job.job_id, head))

    def test_commits_after_the_checks_invalidate_them(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            self.reviewing_with_real_checks(job.job_id, lease.token)
            head = self.commit_work(job.job_id, "def greet():\n    return 'changed'\n")
            with self.assertRaisesRegex(InvalidTransitionError, "commit"):
                self.store.request_merge_approval(
                    job.job_id, lease.token, self.github(job.job_id, head)
                )

    def test_a_mocked_build_cannot_reach_gate_b(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store, job.job_id, role="builder", acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )  # fmt: skip
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            run_builder(self.store, lease, MockProvider("mock", writes={"src/a.py": "a\n"}), task)
            wt = Path(task.worktree_path)
            run_git(wt, "add", "-A")
            run_git(wt, "commit", "-q", "-m", "mock output")
            head = run_git(wt, "rev-parse", "HEAD")
            self.store.run_checks(job.job_id, t, PASSING_CHECK)
            self.store.transition(job.job_id, S.REVIEWING, lease_token=t)
            self.link_draft_pr(job.job_id)
            with self.assertRaisesRegex(InvalidTransitionError, "mocked"):
                self.store.request_merge_approval(job.job_id, t, self.github(job.job_id, head))

    def test_failing_or_dirty_checks_do_not_pass(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            self.commit_work(job.job_id, "x = 1\n")
            self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
            failed = self.store.run_checks(job.job_id, t, ((sys.executable, "-c", "exit(3)"),))
            self.assertEqual(failed.status, "failed")
            job_now = self.store.load(job.job_id)
            assert job_now.worktree is not None
            (Path(job_now.worktree.path) / "uncommitted.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(InvalidTransitionError, "uncommitted"):
                self.store.run_checks(job.job_id, t, PASSING_CHECK)
            with self.assertRaises(LeaseError):
                self.store.run_checks(job.job_id, "forged", PASSING_CHECK)
            self.store.transition(job.job_id, S.NEEDS_HUMAN, lease_token=t, reason="red")


# --- 5. approved-plan mismatch -----------------------------------------------------------------


class ApprovedPlanTests(GitRepoTestCase):
    def test_exploit_builder_receives_exactly_the_approved_plan(self) -> None:
        # The approved plan is an uncommitted edit in the main checkout; the worktree is built
        # from origin/main and therefore holds a different docs/plan.md.
        approved = "# Plan\r\n\r\nAdd a greeting.\r\nAlso log it. ✓\n"
        (self.repo / "docs" / "plan.md").write_bytes(approved.encode("utf-8"))
        job = self.ready_job()
        assert job.worktree is not None
        in_worktree = (Path(job.worktree.path) / "docs" / "plan.md").read_bytes()
        self.assertNotEqual(in_worktree, approved.encode("utf-8"))

        task = build_task(
            self.store, job.job_id, role="builder", acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )  # fmt: skip
        builder = MockProvider("mock")
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            run_builder(self.store, lease, builder, task)
        self.assertEqual(builder.executed[0].plan_text.encode("utf-8"), approved.encode("utf-8"))

    def test_a_task_carrying_other_plan_text_is_refused(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store, job.job_id, role="builder", acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )  # fmt: skip
        forged = replace(task, plan_text="# Do something else\n")
        with self.assertRaises(ValidationError):
            forged.validate()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises((ValidationError, ProviderError)):
                run_builder(self.store, lease, MockProvider("mock"), forged)
        self.assertEqual(self.store.load(job.job_id).state, S.READY)

    def test_a_non_utf8_plan_is_refused_at_submission(self) -> None:
        (self.repo / "docs" / "plan.md").write_bytes(b"\xff\xfe not utf-8")
        job = self.new_job()
        with self.assertRaises(ValidationError):
            self.store.submit_plan(job.job_id, "docs/plan.md")


# --- 6. unbounded timeout ----------------------------------------------------------------------

_ORPHAN_HOLDS_PIPE = """
import subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"],
                     stdout=sys.stdout, stderr=sys.stderr)
open(sys.argv[1], "w").write(str(g.pid))
"""

_ORPHAN_DETACHED = """
import subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(sys.argv[1], "w").write(str(g.pid))
"""


class ProcessCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="forgeflow-proc-"))
        self.addCleanup(rmtree, self.tmp)
        self.pid_file = self.tmp / "grandchild.pid"

    def grandchild(self) -> int:
        pid = int(self.pid_file.read_text())
        self.addCleanup(kill_process_tree, pid)
        return pid

    def test_exploit_timeout_is_bounded_when_a_descendant_outlives_its_parent(self) -> None:
        started = time.monotonic()
        result = run_command(
            [sys.executable, "-c", _ORPHAN_HOLDS_PIPE, str(self.pid_file)], timeout=3
        )
        elapsed = time.monotonic() - started
        pid = self.grandchild()
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 20, "run_command waited for the orphaned grandchild")
        self.assertTrue(_wait_dead(pid), "the orphaned grandchild survived the timeout")

    def test_exploit_descendants_are_cleaned_up_after_a_normal_exit(self) -> None:
        result = run_command([sys.executable, "-c", _ORPHAN_DETACHED, str(self.pid_file)])
        pid = self.grandchild()
        self.assertTrue(result.ok, result.describe())
        self.assertTrue(_wait_dead(pid), "a descendant outlived the command")


# --- 7. allowed-path traversal -----------------------------------------------------------------


class AllowedPathTests(GitRepoTestCase):
    def test_exploit_dotdot_does_not_escape_a_pattern(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store, job.job_id, role="builder", acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )  # fmt: skip
        for path in (
            "src/../.github/workflows/evil.yml",
            "src\\..\\.github\\workflows\\evil.yml",
            "src/./../README.md",
            "src/../../outside.txt",
            "src/.git/config",
            "src/a.py:stream",
            "/src/a.py",
        ):
            with self.subTest(path):
                self.assertFalse(task.permits_path(path))
        self.assertTrue(task.permits_path("src/pkg/a.py"))

        rogue = MockProvider("mock", writes={"src/../.github/workflows/evil.yml": "x"})
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                run_builder(self.store, lease, rogue, task)
        self.assertFalse((Path(task.worktree_path) / ".github").exists())

    def test_a_symlink_inside_an_allowed_directory_does_not_widen_it(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store, job.job_id, role="builder", acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )  # fmt: skip
        wt = Path(task.worktree_path)
        (wt / "src").mkdir()
        (wt / "outside").mkdir()
        try:
            os.symlink(wt / "outside", wt / "src" / "link", target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            # Unprivileged Windows: a directory junction redirects the same way.
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(wt / "src" / "link"), str(wt / "outside")],
                check=True,
                capture_output=True,
            )
        with self.assertRaises(ValidationError):
            task.permitted_target("src/link/evil.py")


# --- 8. credential disclosure ------------------------------------------------------------------

SECRET = "ghp_SECRETtoken0123456789"  # noqa: S105 - a fake token to look for


class CredentialRedactionTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        run_git(
            self.repo, "remote", "set-url", "origin", f"https://pdj:{SECRET}@github.com/o/demo.git"
        )

    def test_exploit_remote_credentials_are_not_persisted_or_displayed(self) -> None:
        from forgeflow.environment import check_repository
        from forgeflow.gitrepo import discover_identity

        ident = discover_identity(self.repo)
        self.assertEqual(ident.slug, "o/demo")
        self.assertEqual(ident.remote_url, "https://github.com/o/demo.git")
        check, _ = check_repository(self.repo, run_command)
        self.assertNotIn(SECRET, repr(check))
        job = self.new_job()
        on_disk = (self.store.job_dir(job.job_id) / "job.json").read_text("utf-8")
        self.assertNotIn(SECRET, on_disk)
        self.assertNotIn("pdj", on_disk)

    def test_a_legacy_record_with_credentials_is_redacted_on_load(self) -> None:
        job = self.new_job()
        self.tamper(
            job.job_id,
            lambda d: d["repo"].update(
                remote_url=f"https://x-access-token:{SECRET}@github.com/o/d"
            ),
        )
        self.assertNotIn(SECRET, str(self.store.load(job.job_id).to_dict()))

    def test_command_diagnostics_are_redacted(self) -> None:
        result = run_command(
            [sys.executable, "-c", f"import sys; sys.exit('fatal: https://u:{SECRET}@host/x')"]
        )
        self.assertNotIn(SECRET, result.describe())


if __name__ == "__main__":
    unittest.main()
