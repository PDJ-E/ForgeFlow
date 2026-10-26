"""Short job references (J1, J2, ...): allocation, no reuse, resolution, compatibility."""

from __future__ import annotations

import io
import json
import threading
from contextlib import redirect_stderr, redirect_stdout

from forgeflow import cli
from forgeflow.errors import InvalidTransitionError, ValidationError
from forgeflow.jobs import Job, Policy
from forgeflow.store import JobStore
from tests.support import GitRepoTestCase, rmtree


class AllocationTests(GitRepoTestCase):
    def test_new_jobs_get_sequential_references(self) -> None:
        refs = [self.new_job().ref for _ in range(3)]
        self.assertEqual(refs, ["J1", "J2", "J3"])

    def test_references_are_never_reused(self) -> None:
        self.new_job()
        second = self.new_job()
        rmtree(self.store.job_dir(second.job_id))  # a job deleted by hand
        self.assertEqual(self.new_job().ref, "J3")

    def test_lost_counter_never_duplicates_a_reference_on_disk(self) -> None:
        self.new_job()
        self.new_job()
        (self.store.runtime_dir / "refs.json").unlink()
        self.assertEqual(self.new_job().ref, "J3")

    def test_concurrent_creation_gets_distinct_references(self) -> None:
        refs: list[str | None] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                store = JobStore.for_repo(self.repo, lock_timeout=30)  # a separate store object
                refs.append(store.create("parallel", self.repo_record(), self.new_policy()).ref)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(refs, key=lambda r: int(r[1:]) if r else 0), [f"J{n}" for n in range(1, 7)]
        )

    def test_reference_cannot_be_changed_by_update(self) -> None:
        job = self.new_job()

        def rename(j: Job) -> None:
            j.ref = "J99"

        with self.assertRaises(InvalidTransitionError):
            self.store.update(job.job_id, rename, event="rename")

    def new_policy(self) -> Policy:
        return Policy("mock", None)


class CompatibilityTests(GitRepoTestCase):
    def strip_ref(self, job_id: str) -> None:
        self.tamper(job_id, lambda data: data.pop("ref"))

    def test_existing_jobs_without_references_still_load(self) -> None:
        job = self.new_job()
        self.strip_ref(job.job_id)
        loaded = self.store.load(job.job_id)
        self.assertIsNone(loaded.ref)
        self.assertEqual(loaded.label, job.job_id)

    def test_backfill_follows_creation_order_and_is_idempotent(self) -> None:
        first, second = self.new_job(), self.new_job()
        for job in (first, second):
            self.strip_ref(job.job_id)
        (self.store.runtime_dir / "refs.json").unlink()  # as before references existed
        assigned = self.store.assign_missing_refs()
        self.assertEqual([j.ref for j in assigned], ["J1", "J2"])
        self.assertEqual(self.store.load(first.job_id).ref, "J1")
        self.assertEqual(self.store.assign_missing_refs(), [])
        self.assertEqual(self.new_job().ref, "J3")
        events = [e["event"] for e in self.store.events(first.job_id)]
        self.assertIn("ref-assigned", events)


class ResolutionTests(GitRepoTestCase):
    def test_full_id_and_reference_in_any_case(self) -> None:
        job = self.new_job()
        for token in (job.job_id, "J1", "j1", " J1 "):
            with self.subTest(token=token):
                self.assertEqual(self.store.resolve(token), job.job_id)

    def test_unknown_malformed_and_ambiguous_are_refused(self) -> None:
        a, b = self.new_job(), self.new_job()
        with self.assertRaisesRegex(ValidationError, r"no job J7 .*known: J1, J2"):
            self.store.resolve("J7")
        for token in ("1", "J0", "T1", "#4", "J1x", "ff-1"):
            with self.subTest(token=token), self.assertRaisesRegex(ValidationError, "neither"):
                self.store.resolve(token)

        def clash(data: dict[str, object]) -> None:
            data["ref"] = "J1"  # a hand-edited record claiming another job's reference

        self.tamper(b.job_id, clash)
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            self.store.resolve("J1")
        self.assertEqual(self.store.resolve(a.job_id), a.job_id)  # full ids still work


class CliReferenceTests(GitRepoTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def test_commands_accept_short_references(self) -> None:
        code, out, _ = self.run_cli("idea", "new", "Greeting", "--idea", "say hello")
        self.assertEqual(code, 0)
        self.assertIn("Started discovery J1 (ff-", out)
        self.assertIn("forgeflow idea add J1 <kind>", out)
        code, out, _ = self.run_cli("idea", "add", "j1", "goal", "Friendly start")
        self.assertEqual((code, out.strip()), (0, "G1 [proposed] Friendly start"))
        code, out, _ = self.run_cli("status", "J1")
        self.assertEqual(code, 0)
        self.assertIn("Job J1 (ff-", out)
        code, out, _ = self.run_cli("status")
        self.assertRegex(out, r"(?m)^REF\s+JOB\s+STATE")
        self.assertRegex(out, r"(?m)^J1\s+ff-")

    def test_status_is_read_only_but_writing_commands_backfill(self) -> None:
        job = self.new_job()
        self.tamper(job.job_id, lambda data: data.pop("ref"))
        before = (self.store.job_dir(job.job_id) / "job.json").read_bytes()
        code, _, err = self.run_cli("status", "J1")
        self.assertEqual(code, 1)  # not assigned yet, and status does not assign it
        self.assertIn("no job J1", err)
        self.assertEqual((self.store.job_dir(job.job_id) / "job.json").read_bytes(), before)
        self.run_cli("status", job.job_id)  # the full id always works
        self.store.submit_plan(job.job_id, "docs/plan.md")
        code, _, err = self.run_cli("design", "revise", job.job_id, "--reason", "edit")
        self.assertEqual(code, 0, err)
        # J1 was handed out once (then lost from the record), so it is not reused.
        self.assertEqual(self.store.load(job.job_id).ref, "J2")
        code, _, _ = self.run_cli("status", "J2")
        self.assertEqual(code, 0)

    def test_unknown_reference_is_an_error_naming_known_ones(self) -> None:
        self.new_job()
        code, _, err = self.run_cli("approve", "J5")
        self.assertEqual(code, 1)
        self.assertIn("known: J1", err)

    def test_json_status_includes_the_reference(self) -> None:
        self.new_job()
        _, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out)["jobs"][0]["ref"], "J1")
