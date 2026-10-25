from __future__ import annotations

import unittest
from typing import Any

from forgeflow.errors import ValidationError
from forgeflow.jobs import (
    SLOT_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    Job,
    JobState,
    Policy,
    Repo,
    branch_for,
    new_job_id,
    utc_now,
    validate_job_id,
)


def _job() -> Job:
    now = utc_now()
    return Job(
        job_id=new_job_id(),
        title="t",
        state=JobState.DRAFT,
        repo=Repo(root="C:/r", slug="o/n", remote_url=None, base_branch="main"),
        policy=Policy("claude", "codex"),
        created_at=now,
        updated_at=now,
        revision=1,
    )


class JobRecordTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        job = _job()
        self.assertEqual(Job.from_dict(job.to_dict()).to_dict(), job.to_dict())

    def test_job_ids_are_path_safe(self) -> None:
        validate_job_id(new_job_id())
        for bad in (
            "../etc",
            "ff-2026-abc",
            "ff-20261024-ABCDEF",
            "ff-20261024-abc/de",
            "ff-20261024-abcdef\\x",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_job_id(bad)
        self.assertTrue(branch_for(new_job_id()).startswith("forge/ff-"))

    def _invalid(self, mutate: Any) -> None:
        data = _job().to_dict()
        mutate(data)
        with self.assertRaises(ValidationError):
            Job.from_dict(data)

    def test_rejects_malformed_records(self) -> None:
        cases = {
            "unknown state": lambda d: d.update(state="exploding"),
            "schema version": lambda d: d.update(schema_version=99),
            "bool as int": lambda d: d.update(revision=True),
            "negative revision": lambda d: d.update(revision=-1),
            "missing repo": lambda d: d.pop("repo"),
            "bad slug": lambda d: d["repo"].update(slug="not a slug"),
            "bad provider": lambda d: d["policy"].update(builder="Rm -rf"),
            "bad id": lambda d: d.update(job_id="../../x"),
            "empty title": lambda d: d.update(title="  "),
            "bad plan hash": lambda d: d["plan"].update(sha256="xyz"),
            "interrupted without origin": lambda d: d.update(state="interrupted"),
            "approval without approver": lambda d: d["plan"].update(
                approval={
                    "gate": "plan",
                    "approver": " ",
                    "approved_at": "x",
                    "method": "m",
                    "subject_sha256": "0" * 64,
                }
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self._invalid(mutate)

    def test_not_a_dict(self) -> None:
        with self.assertRaises(ValidationError):
            Job.from_dict(["not", "a", "job"])


class TransitionTableTests(unittest.TestCase):
    def test_every_state_has_an_entry(self) -> None:
        self.assertEqual(set(TRANSITIONS), set(JobState))

    def test_terminal_states_have_no_exits(self) -> None:
        for state in TERMINAL_STATES:
            self.assertEqual(TRANSITIONS[state], frozenset())

    def test_draft_cannot_reach_execution_directly(self) -> None:
        # AC-02: discussion-only work has no edge into anything that writes code.
        self.assertEqual(
            TRANSITIONS[JobState.DRAFT], {JobState.AWAITING_PLAN_APPROVAL, JobState.CANCELLED}
        )

    def test_only_merge_approval_state_leads_to_merged(self) -> None:
        sources = {s for s, targets in TRANSITIONS.items() if JobState.MERGED in targets}
        self.assertEqual(sources, {JobState.AWAITING_MERGE_APPROVAL})

    def test_slot_states_exclude_unapproved_and_terminal(self) -> None:
        for state in (JobState.DRAFT, JobState.AWAITING_PLAN_APPROVAL, *TERMINAL_STATES):
            self.assertNotIn(state, SLOT_STATES)


if __name__ == "__main__":
    unittest.main()
