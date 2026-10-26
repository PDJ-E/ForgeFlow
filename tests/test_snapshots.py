"""Plan snapshots: short per-version names, integrity, compatibility and deep Windows paths."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forgeflow import fsutil
from forgeflow.approval import approve_plan
from forgeflow.errors import InvalidTransitionError, PathTooLongError
from forgeflow.jobs import JobState, Policy, Repo
from forgeflow.store import JobStore
from tests.support import GitRepoTestCase, ScriptedConsole, rmtree

PLAN_1 = b"# Plan\n\nAdd a greeting.\n"  # docs/plan.md as GitRepoTestCase writes it
PLAN_2 = b"# Plan\n\nAdd a greeting, twice.\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SnapshotTests(GitRepoTestCase):
    def plans_dir(self, job_id: str) -> Path:
        return self.store.job_dir(job_id) / "plans"

    def write_plan(self, data: bytes) -> None:
        (self.repo / "docs" / "plan.md").write_bytes(data)

    def two_versions(self) -> str:
        self.write_plan(PLAN_1)
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        self.store.transition(job.job_id, JobState.DRAFT, reason="revise")
        self.write_plan(PLAN_2)
        self.store.submit_plan(job.job_id, "docs/plan.md")
        return job.job_id

    def test_each_version_gets_a_short_immutable_file(self) -> None:
        job_id = self.two_versions()
        files = sorted(p.name for p in self.plans_dir(job_id).iterdir())
        self.assertEqual(files, ["v1.md", "v2.md"])  # no hash-named files any more
        self.assertEqual((self.plans_dir(job_id) / "v1.md").read_bytes(), PLAN_1)
        self.assertEqual((self.plans_dir(job_id) / "v2.md").read_bytes(), PLAN_2)
        job = self.store.load(job_id)
        self.assertEqual((job.plan.version, job.plan.sha256), (2, sha(PLAN_2)))
        submitted = [
            (e["details"]["version"], e["details"]["sha256"])
            for e in self.store.events(job_id)
            if e["event"] == "plan-submitted"
        ]
        self.assertEqual(submitted, [(1, sha(PLAN_1)), (2, sha(PLAN_2))])  # full hashes kept

    def test_approval_reads_its_own_version(self) -> None:
        job_id = self.two_versions()
        approved = self.approve(job_id)
        self.assertEqual(self.store.approved_plan_text(approved), PLAN_2.decode())
        self.assertEqual(self.store.plan_snapshot_text(job_id, sha(PLAN_1), 1), PLAN_1.decode())

    def test_modified_snapshot_is_refused_before_and_after_approval(self) -> None:
        job_id = self.two_versions()
        snapshot = self.plans_dir(job_id) / "v2.md"
        snapshot.write_bytes(b"# Plan\n\nSomething else.\n")
        with self.assertRaisesRegex(InvalidTransitionError, "v2.md was modified"):
            self.approve(job_id)
        snapshot.write_bytes(PLAN_2)
        job = self.approve(job_id)
        snapshot.write_bytes(PLAN_2 + b"\nDelete production.\n")
        with self.assertRaisesRegex(InvalidTransitionError, "v2.md was modified"):
            self.store.verify_plan_approval(job.job_id)
        with self.assertRaisesRegex(InvalidTransitionError, "modified"):
            self.store.approved_plan_text(job)

    def test_a_modified_version_file_never_falls_back_to_an_older_name(self) -> None:
        self.write_plan(PLAN_1)
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        legacy = self.plans_dir(job.job_id) / f"{sha(PLAN_1)}.md"
        legacy.write_bytes(PLAN_1)  # an intact copy under the old name
        (self.plans_dir(job.job_id) / "v1.md").write_bytes(b"tampered")
        with self.assertRaisesRegex(InvalidTransitionError, "v1.md was modified"):
            self.store.plan_snapshot_text(job.job_id, sha(PLAN_1), 1)

    def test_missing_snapshot_is_refused(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        (self.plans_dir(job.job_id) / "v1.md").unlink()
        with self.assertRaisesRegex(InvalidTransitionError, "snapshot is missing"):
            self.approve(job.job_id)

    def test_existing_hash_named_snapshots_still_work(self) -> None:
        """Jobs written before this change keep plans/<sha256>.md and stay approvable/valid."""
        self.write_plan(PLAN_1)
        job = self.approved_job()
        assert job.plan.sha256 == sha(PLAN_1)
        v1 = self.plans_dir(job.job_id) / "v1.md"
        v1.rename(self.plans_dir(job.job_id) / f"{sha(PLAN_1)}.md")
        self.assertEqual(self.store.approved_plan_text(job), PLAN_1.decode())
        self.store.verify_plan_approval(job.job_id)

        # A Checkpoint A record has no plan or approval version at all.
        def strip_versions(data: dict[str, object]) -> None:
            plan = data["plan"]
            assert isinstance(plan, dict)
            plan["version"] = 0
            plan["approval"]["subject_version"] = None

        self.tamper(job.job_id, strip_versions)
        old = self.store.load(job.job_id)
        self.assertEqual(self.store.approved_plan_text(old), PLAN_1.decode())

    def test_snapshot_left_by_an_interrupted_submission_is_replaced(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        self.store.transition(job.job_id, JobState.DRAFT, reason="revise")
        orphan = self.plans_dir(job.job_id) / "v2.md"
        orphan.write_bytes(b"written, then the process died before job.json recorded v2")
        v1_before = (self.plans_dir(job.job_id) / "v1.md").read_bytes()
        self.write_plan(PLAN_2)
        job = self.store.submit_plan(job.job_id, "docs/plan.md")
        self.assertEqual(job.plan.version, 2)
        self.assertEqual(orphan.read_bytes(), PLAN_2)
        self.assertEqual((self.plans_dir(job.job_id) / "v1.md").read_bytes(), v1_before)


def _deep_dir(base: Path, length: int) -> Path:
    """A directory under ``base`` whose absolute path has exactly ``length`` characters."""
    path = Path(os.path.abspath(base))
    while len(str(path)) < length:
        room = length - len(str(path)) - 1
        path = path / ("d" * min(40, room) if room > 0 else "")
        if room <= 0:
            break
    if len(str(path)) != length:  # pragma: no cover - base already too long
        raise unittest.SkipTest(f"temporary directory {base} is too long for this test")
    path.mkdir(parents=True)
    return path


class DeepPathTests(unittest.TestCase):
    """Repositories nested deep in the file system, under Windows' 259-character limit."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="ffd"))
        self.addCleanup(rmtree, self.base)

    def submit_and_approve(self, root: Path) -> str:
        store = JobStore.for_repo(root, lock_timeout=5)
        repo = Repo(root=str(root), slug="example/demo", remote_url=None, base_branch="main")
        job = store.create("Deep", repo, Policy("mock", None))
        (root / "plan.md").write_bytes(PLAN_1)
        store.submit_plan(job.job_id, "plan.md")
        with mock.patch("forgeflow.approval.open_human_console", return_value=ScriptedConsole()):
            approved = approve_plan(store, job.job_id)
        self.assertEqual(store.approved_plan_text(approved), PLAN_1.decode())
        return job.job_id

    def test_deep_repository_works_where_hash_names_did_not_fit(self) -> None:
        # The longest name ForgeFlow writes in a job directory is the temporary file of
        # discovery.json, about 72 characters below the root. 185 leaves that under 259.
        root = _deep_dir(self.base, 185)
        job_id = self.submit_and_approve(root)
        snapshot = root / ".forge" / "runtime" / "jobs" / job_id / "plans" / "v1.md"
        self.assertTrue(snapshot.is_file())
        old_temp_name = f".{sha(PLAN_1)}.md.12345678.tmp"
        self.assertGreater(len(str(snapshot.parent / old_temp_name)), fsutil.WINDOWS_MAX_PATH)

    @unittest.skipUnless(sys.platform == "win32", "Windows path limit")
    def test_too_deep_fails_clearly_and_writes_nothing(self) -> None:
        root = _deep_dir(self.base, 205)  # job.json's temporary file would need ~267
        store = JobStore.for_repo(root, lock_timeout=5)
        repo = Repo(root=str(root), slug=None, remote_url=None, base_branch="main")
        try:
            store.create("Too deep", repo, Policy("mock", None))
        except PathTooLongError as exc:
            self.assertIn("Nothing was written", str(exc))
            jobs = store.list_jobs()[0]
            self.assertEqual(jobs, [])  # no job record, nothing half-written
            leftovers = [p for p in (root / ".forge").rglob("*.tmp")]
            self.assertEqual(leftovers, [])
        else:  # long paths are enabled on this machine: the write is simply allowed
            self.assertEqual(len(store.list_jobs()[0]), 1)


class LongPathMessageTests(unittest.TestCase):
    """The explanation itself, on every platform (the check is Windows-only at run time)."""

    def test_only_over_long_paths_are_reported(self) -> None:
        error = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(sys, "platform", "win32"):
            long_path = Path("C:/" + "x" * 240) / "job.json"
            explained = fsutil._explain_long_path(long_path, 30, error)
            self.assertIsInstance(explained, PathTooLongError)
            self.assertIn("LongPathsEnabled", str(explained))
            short = fsutil._explain_long_path(Path("C:/repo/job.json"), 30, error)
            self.assertIs(short, error)  # an ordinary error is passed through unchanged
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIs(fsutil._explain_long_path(long_path, 30, error), error)

    def test_failed_atomic_write_keeps_the_old_file(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ffl"))
        self.addCleanup(rmtree, tmp)
        target = tmp / "job.json"
        target.write_text(json.dumps({"keep": True}), encoding="utf-8")
        with (
            mock.patch.object(tempfile, "mkstemp", side_effect=FileNotFoundError(2, "x")),
            mock.patch.object(fsutil, "WINDOWS_MAX_PATH", 10),
            mock.patch.object(sys, "platform", "win32"),
            self.assertRaises(PathTooLongError),
        ):
            fsutil.atomic_write_json(target, {"keep": False})
        self.assertEqual(json.loads(target.read_text("utf-8")), {"keep": True})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
