"""The single job-state -> Project Status mapping and its idempotent sync.

REAL: the job store and transitions. MOCKED: GitHub (FakeGitHubWorld).
"""

from __future__ import annotations

import unittest

from forgeflow.board import STATUS_FOR_STATE, BoardListener, status_for, sync
from forgeflow.errors import GitHubError
from forgeflow.jobs import JobState
from tests.fake_github import FakeGitHubWorld
from tests.pipeline_support import PipelineTestCase

S = JobState


class MappingTests(unittest.TestCase):
    def test_every_state_has_exactly_one_decision(self) -> None:
        self.assertEqual(set(STATUS_FOR_STATE), set(JobState))

    def test_done_and_blocked_have_one_source_each(self) -> None:
        self.assertEqual([s for s, v in STATUS_FOR_STATE.items() if v == "Done"], [S.MERGED])
        self.assertEqual(
            [s for s, v in STATUS_FOR_STATE.items() if v == "Blocked"], [S.NEEDS_HUMAN]
        )

    def test_lifecycle_order(self) -> None:
        order = [S.READY, S.BUILDING, S.VERIFYING, S.REVIEWING, S.AWAITING_MERGE_APPROVAL]
        self.assertEqual(
            [STATUS_FOR_STATE[s] for s in order], ["Ready", "Building", "QA", "Review", "Review"]
        )
        for transient in (S.INTERRUPTED, S.CANCELLED, S.DRAFT, S.AWAITING_PLAN_APPROVAL):
            self.assertIsNone(STATUS_FOR_STATE[transient])


class SyncTests(PipelineTestCase):
    def test_ready_uses_the_status_chosen_at_init(self) -> None:
        from dataclasses import replace

        self.assertEqual(status_for(S.READY, replace(self.binding, ready_status="Todo")), "Todo")

    def test_sync_is_idempotent(self) -> None:
        job = self.handed_off_job()
        self.tamper(job.job_id, lambda d: d.update(state="needs_human"))
        first = sync(self.store, job.job_id, self.client, self.binding)
        self.assertEqual((first.before, first.after, first.changed), ("Ready", "Blocked", True))
        writes = list(self.world.writes)
        second = sync(self.store, job.job_id, self.client, self.binding)
        self.assertFalse(second.changed)
        self.assertEqual(self.world.writes, writes, "a converged board is not written again")
        record = self.store.load(job.job_id).github.board
        assert record is not None
        self.assertEqual(
            (record.state, record.status, record.error), ("needs_human", "Blocked", None)
        )

    def test_a_status_moved_by_hand_is_restored_and_logged(self) -> None:
        job = self.handed_off_job()
        assert job.github.issue is not None
        item = self.world.item_for(job.github.issue.number)
        assert item is not None
        item["status"] = "Done"  # someone dragged the card
        result = sync(self.store, job.job_id, self.client, self.binding)
        self.assertEqual((result.before, result.after), ("Done", "Ready"))
        event = [e for e in self.store.events(job.job_id) if e["event"] == "board-synced"][-1]
        self.assertEqual(event["details"]["from"], "Done")

    def test_a_missing_option_is_reported_not_invented(self) -> None:
        self.world.statuses = ("Backlog", "Ready", "Done")
        job = self.handed_off_job()
        self.tamper(job.job_id, lambda d: d.update(state="needs_human"))
        with self.assertRaisesRegex(GitHubError, "no Status option 'Blocked'"):
            sync(self.store, job.job_id, self.client, self.binding)

    def test_the_listener_records_failures_and_never_blocks_a_transition(self) -> None:
        job = self.handed_off_job()
        listener = BoardListener(self.store, self.client, self.binding)
        self.store.listeners.append(listener)
        self.world.offline = True
        job = self.store.transition(job.job_id, S.CANCELLED, reason="test")
        self.assertEqual(job.state, S.CANCELLED)  # cancelled maps to "unchanged": no call at all
        self.tamper(job.job_id, lambda d: d.update(state="plan_approved"))
        self.store.transition(job.job_id, S.CANCELLED, reason="again")
        self.assertEqual(listener.errors, [])
        self.world.offline = False

    def test_offline_sync_is_recorded_for_a_retry(self) -> None:
        job = self.handed_off_job()
        listener = BoardListener(self.store, self.client, self.binding)
        self.world.offline = True
        listener(self.store.load(job.job_id))
        self.assertTrue(listener.errors)
        record = self.store.load(job.job_id).github.board
        assert record is not None and record.error is not None
        self.assertIn("unreachable", record.error)
        self.world.offline = False
        listener(self.store.load(job.job_id))
        record = self.store.load(job.job_id).github.board
        assert record is not None
        self.assertIsNone(record.error)

    def test_an_unbound_project_is_refused(self) -> None:
        from dataclasses import replace

        job = self.handed_off_job()
        other = replace(self.binding, project_id="PVT_other")
        with self.assertRaisesRegex(GitHubError, "no longer the Project"):
            sync(self.store, job.job_id, self.client, other)


class WorldDefaultsTests(unittest.TestCase):
    def test_the_fake_board_has_the_real_projects_options(self) -> None:
        # ForgeFlow Development (Project #2), read on 2026-10-26.
        self.assertEqual(
            tuple(FakeGitHubWorld().statuses or ()),
            ("Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done"),
        )


if __name__ == "__main__":
    unittest.main()
