"""Engineering design: scaffold from discovery, validation, versions, revision, hashing."""

from __future__ import annotations

import hashlib

from forgeflow import design, discovery
from forgeflow.errors import InvalidTransitionError, ValidationError
from forgeflow.jobs import IssueLink, Job, JobState
from tests.support import GitRepoTestCase

VALID_PLAN = """# Greeting on start-up

## Problem and objective

Users are not greeted.

## Requirements

- Print hello.

## Proposed solution

A greet() function.

## Alternatives and trade-offs

- Banner: rejected.

## Scope

Only the greeting.

## Dependencies and risks

None.

## Acceptance criteria

- [ ] hello is printed once

## Verification

- unit test

## Tasks

### T1: Add greet()

Change main.py.

- [ ] greet() prints hello
- [ ] main() calls greet()

### T2: Document it

- [ ] README mentions the greeting
"""


class PlanCheckTests(GitRepoTestCase):
    def test_valid_plan(self) -> None:
        result = design.check_text(VALID_PLAN)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.title, "Greeting on start-up")
        self.assertEqual([(t.id, t.criteria) for t in result.tasks], [("T1", 2), ("T2", 1)])

    def test_structural_errors(self) -> None:
        cases = {
            "exactly one '# Title'": VALID_PLAN.replace("# Greeting on start-up\n", ""),
            "missing section '## Verification'": VALID_PLAN.replace("## Verification", "## Checks"),
            "task T2 has no '- [ ]'": VALID_PLAN.replace("- [ ] README", "- README"),
            "appears twice": VALID_PLAN.replace("### T2:", "### T1:"),
            "is not '### T<n>: <title>'": VALID_PLAN.replace("### T2: Document it", "### Docs"),
            "unresolved TODO(forgeflow)": VALID_PLAN + "\nTODO(forgeflow): later\n",
            "GitHub issue bodies": VALID_PLAN + "x" * design.MAX_PLAN_CHARS,
            "has no '### T<n>: <title>' task": VALID_PLAN.split("### T1")[0],
        }
        for expected, text in cases.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in e for e in design.check_text(text).errors))

    def test_recommended_sections_and_local_paths_are_warnings(self) -> None:
        text = VALID_PLAN.replace("## Proposed solution", "## Idea").replace(
            "Change main.py.", r"See C:\Users\someone\notes.txt."
        )
        result = design.check_text(text)
        self.assertTrue(result.ok)
        self.assertTrue(any("Proposed solution" in w for w in result.warnings))
        self.assertTrue(any("local user path" in w for w in result.warnings))


class DesignWorkflowTests(GitRepoTestCase):
    def draft(self) -> str:
        job, _ = discovery.start(self.store, "Greeting", "greet users", self.repo_record())
        discovery.add(self.store, job.job_id, "problem", "No greeting", status="accepted")
        discovery.add(self.store, job.job_id, "requirement", "Print hello", status="accepted")
        discovery.add(self.store, job.job_id, "alternative", "Banner")
        discovery.mark(self.store, job.job_id, "O1", "rejected", note="noisy")
        discovery.add(self.store, job.job_id, "non-goal", "Localisation")
        return job.job_id

    def write_plan(self, job_id: str, text: str = VALID_PLAN) -> None:
        # Bytes, not write_text: Windows would turn \n into \r\n and change the hash.
        design.plan_file(self.store, job_id).write_bytes(text.encode("utf-8"))

    def test_scaffold_carries_discovery_and_never_overwrites(self) -> None:
        job_id = self.draft()
        path, created = design.scaffold(self.store, job_id)
        self.assertTrue(created)
        text = path.read_text(encoding="utf-8")
        for expected in (
            "# Greeting",
            "- No greeting",
            "- Print hello",
            "Banner (rejected: noisy)",
        ):
            self.assertIn(expected, text)
        self.assertIn("- Localisation", text)
        self.assertFalse(design.check(self.store, job_id).ok)  # placeholders must be resolved
        path.write_text("# mine\n", encoding="utf-8")
        _, created = design.scaffold(self.store, job_id)
        self.assertFalse(created)
        self.assertEqual(path.read_text(encoding="utf-8"), "# mine\n")

    def test_submit_refuses_an_incomplete_plan(self) -> None:
        job_id = self.draft()
        design.scaffold(self.store, job_id)
        with self.assertRaisesRegex(ValidationError, "not ready to submit"):
            design.submit(self.store, job_id)
        self.assertIs(self.store.load(job_id).state, JobState.DRAFT)

    def test_versions_and_content_hash(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id)
        job, result = design.submit(self.store, job_id)
        self.assertTrue(result.ok)
        self.assertEqual(job.plan.version, 1)
        self.assertEqual(job.plan.sha256, hashlib.sha256(VALID_PLAN.encode()).hexdigest())
        self.assertEqual(job.plan.path, design.plan_relpath(job_id))
        # Resubmitting a changed plan supersedes the pending version with a new one.
        self.write_plan(job_id, VALID_PLAN.replace("hello", "hi"))
        job, _ = design.submit(self.store, job_id)
        self.assertEqual(job.plan.version, 2)
        self.assertNotEqual(job.plan.sha256, hashlib.sha256(VALID_PLAN.encode()).hexdigest())
        # Both snapshots are kept, and the log names each version's hash.
        self.assertEqual(len(list((self.store.job_dir(job_id) / "plans").glob("*.md"))), 2)
        versions = [
            e["details"]["version"]
            for e in self.store.events(job_id)
            if e["event"] == "plan-submitted"
        ]
        self.assertEqual(versions, [1, 2])

    def test_revise_withdraws_an_approval_before_handoff(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id)
        design.submit(self.store, job_id)
        approved = self.approve(job_id)
        self.assertIs(approved.state, JobState.PLAN_APPROVED)
        with self.assertRaises(ValidationError):
            design.revise(self.store, job_id, "  ")
        job = design.revise(self.store, job_id, "add a task")
        self.assertIs(job.state, JobState.DRAFT)
        self.assertIsNone(job.plan.approval)
        self.assertEqual(job.plan.version, 1)  # the counter survives; next submission is v2
        job, _ = design.submit(self.store, job_id)
        self.assertEqual(job.plan.version, 2)

    def test_revise_is_refused_after_handoff(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id)
        design.submit(self.store, job_id)
        self.approve(job_id)

        def link(job: Job) -> None:
            job.github.issue = IssueLink("example/demo", 5, "u", "I_5", "t", "OPEN")

        self.store.update(job_id, link, event="issue-linked")
        with self.assertRaisesRegex(InvalidTransitionError, "already handed off"):
            design.revise(self.store, job_id, "too late")

    def test_scaffold_only_in_draft(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id)
        design.submit(self.store, job_id)
        with self.assertRaisesRegex(InvalidTransitionError, "revise it first"):
            design.scaffold(self.store, job_id)
