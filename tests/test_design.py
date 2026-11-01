"""Engineering design: scaffold from discovery, validation, versions, revision, hashing."""

from __future__ import annotations

import hashlib

from forgeflow import design, discovery, pipeline
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

- **Files:** `main.py`,
  `tests/test_main.py`
- [ ] greet() prints hello
- [ ] main() calls greet()

### T2: Document it

- Files: README.md
- [ ] README mentions the greeting
"""
# Approved before plans needed a Files: line per task.
LEGACY_PLAN = VALID_PLAN.replace("- **Files:** `main.py`,\n  `tests/test_main.py`\n", "").replace(
    "- Files: README.md\n", ""
)


def sized_plan(*tasks: tuple[int, int]) -> str:
    """VALID_PLAN with its tasks replaced by (criteria, files) tasks."""
    head = VALID_PLAN.split("### T1")[0]
    body = ""
    for n, (criteria, files) in enumerate(tasks, start=1):
        paths = ", ".join(f"`src/f{i}.py`" for i in range(files))
        checks = "".join(f"- [ ] criterion {i}\n" for i in range(criteria))
        body += f"### T{n}: Task {n}\n\n- **Files:** {paths}\n{checks}\n"
    return head + body


class PlanCheckTests(GitRepoTestCase):
    def test_valid_plan(self) -> None:
        result = design.check_text(VALID_PLAN)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.title, "Greeting on start-up")
        self.assertEqual([(t.id, t.criteria) for t in result.tasks], [("T1", 2), ("T2", 1)])
        self.assertEqual([t.files for t in result.tasks], [2, 1])
        self.assertIn("  T1: Add greet() (2 criteria, 2 files)", result.summary_lines())

    def test_size_contract_at_and_just_over_each_limit(self) -> None:
        n, c, f = design.MAX_TASKS, design.MAX_CRITERIA_PER_TASK, design.MAX_FILES_PER_TASK
        self.assertEqual(design.check_text(sized_plan(*[(c, f)] * n)).errors, [])
        over = {
            f"{n + 1} tasks": sized_plan(*[(1, 1)] * (n + 1)),
            f"{c + 1} acceptance criteria": sized_plan((c + 1, 1)),
            f"lists {f + 1} files": sized_plan((1, f + 1)),
            "T2 has no 'Files:' line": LEGACY_PLAN,
        }
        for expected, text in over.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in e for e in design.check_text(text).errors))

    def test_files_lines_are_counted_as_written(self) -> None:
        def count(files_line: str) -> list[int]:
            text = VALID_PLAN.replace("- Files: README.md\n", files_line)
            return [t.files for t in design.check_text(text).tasks]

        cases = {
            "- Files: `a.py`, b.py, c.py\n": 3,  # mixed: every entry counts
            "- **Files:** `a.py`\n  - [ ] uses `b.py`, `c.py`\n  - [ ] and `d.py`\n": 1,
            "- **Files:** `a.py`,\n  `b.py`\n- [ ] more\n": 2,  # continuation line
        }
        for line, expected in cases.items():
            with self.subTest(line):
                self.assertEqual(count(line)[1], expected)
        for empty in ("- **Files:**\n", "Files:   \n"):
            with self.subTest(empty):
                errors = design.check_text(VALID_PLAN.replace("- Files: README.md\n", empty)).errors
                self.assertTrue(any("T2 has no 'Files:' line" in e for e in errors), errors)

    def test_a_large_plan_is_a_warning_below_the_hard_limit(self) -> None:
        def padded(size: int) -> str:
            return VALID_PLAN.replace("Only the greeting.", "x" * (size - len(VALID_PLAN) + 18))

        at, over = padded(design.PLAN_SIZE_WARNING), padded(design.PLAN_SIZE_WARNING + 1)
        limit = design.PLAN_SIZE_WARNING
        self.assertEqual((len(at), len(over)), (limit, limit + 1))
        self.assertFalse(any("characters" in w for w in design.check_text(at).warnings))
        result = design.check_text(over)
        self.assertTrue(result.ok)
        self.assertTrue(any("split" in w for w in result.warnings))
        self.assertLess(design.PLAN_SIZE_WARNING, design.MAX_PLAN_CHARS)

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


class ProjectNumberWarningTests(GitRepoTestCase):
    def test_project_hash_numbers_are_flagged(self) -> None:
        for text in ("on Project #2 in Ready", "the GitHub Project ForgeFlow Development #2"):
            with self.subTest(text=text):
                result = design.check_text(VALID_PLAN.replace("Only the greeting.", text))
                self.assertTrue(any("links to issue or pull request" in w for w in result.warnings))

    def test_issue_references_and_project_urls_are_fine(self) -> None:
        text = VALID_PLAN.replace(
            "Only the greeting.",
            "Follows issue #12. Board: [Dev](https://github.com/users/x/projects/2).",
        )
        self.assertEqual(design.check_text(text).warnings, [])


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

    def test_an_approved_plan_without_files_lines_still_loads_and_displays(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id, LEGACY_PLAN)
        # Submitted and approved before the size contract existed (submit would now refuse it).
        self.store.submit_plan(job_id, design.plan_relpath(job_id))
        job = self.approve(job_id)
        text = self.store.approved_plan_text(job)
        self.assertEqual(text, LEGACY_PLAN)
        shown = design.check_text(text)
        self.assertEqual(shown.title, "Greeting on start-up")
        self.assertIn("  T2: Document it (1 criteria, 0 files)", shown.summary_lines())
        self.assertEqual(
            pipeline.acceptance_criteria(text, "x"),
            ("hello is printed once", "greet() prints hello", "main() calls greet()",
             "README mentions the greeting"),
        )  # fmt: skip

    def test_scaffold_only_in_draft(self) -> None:
        job_id = self.draft()
        self.write_plan(job_id)
        design.submit(self.store, job_id)
        with self.assertRaisesRegex(InvalidTransitionError, "revise it first"):
            design.scaffold(self.store, job_id)
