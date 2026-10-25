"""Discovery sessions: durable outcomes, status history, resumption, draft-only editing."""

from __future__ import annotations

import json
from unittest import mock

from forgeflow import discovery, fsutil
from forgeflow.errors import InvalidTransitionError, ValidationError
from forgeflow.jobs import UNASSIGNED_PROVIDER, JobState
from forgeflow.store import JobStore
from tests.support import GitRepoTestCase


class DiscoveryTests(GitRepoTestCase):
    def start(self) -> str:
        job, _ = discovery.start(
            self.store, "Greeting", "Users should be greeted", self.repo_record()
        )
        return job.job_id

    def test_start_creates_a_discussion_only_draft(self) -> None:
        job_id = self.start()
        job = self.store.load(job_id)
        self.assertIs(job.state, JobState.DRAFT)
        self.assertEqual(job.policy.builder, UNASSIGNED_PROVIDER)
        self.assertIsNone(job.github.issue)
        found = discovery.load(self.store, job_id)
        self.assertEqual(found.idea, "Users should be greeted")
        self.assertEqual(found.items, [])

    def test_outcomes_persist_and_resume_in_a_new_session(self) -> None:
        job_id = self.start()
        discovery.add(self.store, job_id, "problem", "No greeting", status="accepted")
        discovery.add(self.store, job_id, "user", "CLI users")
        discovery.add(self.store, job_id, "question", "Which language?")
        # A new process (new store object) resumes from disk, not from conversation memory.
        resumed = discovery.load(JobStore.for_repo(self.repo), job_id)
        self.assertEqual([i.id for i in resumed.items], ["P1", "U1", "Q1"])
        text = discovery.render(resumed)
        self.assertIn("P1 [accepted] No greeting", text)
        self.assertIn("Q1 [proposed] Which language?", text)

    def test_reject_refine_and_return_to_an_earlier_decision(self) -> None:
        job_id = self.start()
        discovery.add(self.store, job_id, "decision", "Print hello", status="accepted")
        discovery.add(self.store, job_id, "alternative", "Banner")
        discovery.mark(self.store, job_id, "O1", "rejected", note="too noisy")
        refined = discovery.add(
            self.store, job_id, "decision", "Print hello once", status="accepted", supersedes="D1"
        )
        self.assertEqual((refined.id, refined.supersedes), ("D2", "D1"))
        found = discovery.load(self.store, job_id)
        self.assertEqual(found.get("D1").status, "superseded")
        self.assertEqual([i.id for i in found.live("decision")], ["D2"])

        # Going back: D1 becomes live again and its replacement is retired, nothing is deleted.
        discovery.mark(self.store, job_id, "D1", "accepted", note="simpler after all")
        found = discovery.load(self.store, job_id)
        self.assertEqual(found.get("D1").status, "accepted")
        self.assertEqual(found.get("D2").status, "superseded")
        statuses = [h["status"] for h in found.get("D1").history]
        self.assertEqual(statuses, ["accepted", "superseded", "accepted"])
        self.assertNotIn("Banner", discovery.render(found))
        self.assertIn(
            "O1 [rejected] Banner - too noisy", discovery.render(found, include_closed=True)
        )

    def test_a_rejected_option_can_be_recorded_directly(self) -> None:
        job_id = self.start()
        item = discovery.add(
            self.store,
            job_id,
            "alternative",
            "Use atexit",
            status="rejected",
            note="fires on errors",
        )
        self.assertEqual((item.status, item.note), ("rejected", "fires on errors"))
        self.assertEqual(discovery.load(self.store, job_id).live("alternative"), [])

    def test_every_change_is_audited_and_advances_the_job_revision(self) -> None:
        job_id = self.start()
        before = self.store.load(job_id).revision
        discovery.add(self.store, job_id, "goal", "Friendly start")
        self.assertEqual(self.store.load(job_id).revision, before + 1)
        self.assertEqual(self.store.events(job_id)[-1]["event"], "discovery-item-added")

    def test_rejects_bad_input(self) -> None:
        job_id = self.start()
        with self.assertRaises(ValidationError):
            discovery.add(self.store, job_id, "wish", "x")
        with self.assertRaises(ValidationError):
            discovery.add(self.store, job_id, "goal", "   ")
        with self.assertRaises(ValidationError):
            discovery.add(self.store, job_id, "goal", "x" * 3000)
        with self.assertRaises(ValidationError):
            discovery.mark(self.store, job_id, "G9", "accepted")
        discovery.add(self.store, job_id, "goal", "One")
        with self.assertRaises(ValidationError):
            discovery.add(self.store, job_id, "risk", "Two", supersedes="G1")

    def test_discovery_is_editable_only_while_draft(self) -> None:
        job_id = self.start()
        (self.store.job_dir(job_id) / "plan.md").write_text("# P\n", encoding="utf-8")
        self.store.submit_plan(job_id, f".forge/runtime/jobs/{job_id}/plan.md")
        with self.assertRaisesRegex(InvalidTransitionError, "can change only in: draft"):
            discovery.add(self.store, job_id, "goal", "late idea")

    def test_interrupted_write_keeps_the_previous_record(self) -> None:
        job_id = self.start()
        discovery.add(self.store, job_id, "goal", "Kept")
        path = self.store.job_dir(job_id) / discovery.DOCUMENT
        before = path.read_text(encoding="utf-8")
        with (
            mock.patch.object(fsutil, "_replace_with_retry", side_effect=OSError("power cut")),
            self.assertRaises(OSError),
        ):
            discovery.add(self.store, job_id, "goal", "Lost")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual([i.text for i in discovery.load(self.store, job_id).items], ["Kept"])

    def test_discovery_stays_in_the_self_ignored_runtime(self) -> None:
        job_id = self.start()
        path = self.store.job_dir(job_id) / discovery.DOCUMENT
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text("utf-8"))["job_id"], job_id)
        from tests.support import run_git

        self.assertEqual(run_git(self.repo, "status", "--porcelain"), "")
