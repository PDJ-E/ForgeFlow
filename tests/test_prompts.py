"""The builder prompt: one plan task in full plus the plan's frame (ADR 0006 §1)."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from forgeflow import prompts
from forgeflow.errors import ValidationError
from forgeflow.providers import ProviderTask, build_task
from tests.support import GitRepoTestCase
from tests.test_design import VALID_PLAN


def task_for(plan: str, **fields: object) -> ProviderTask:
    task = ProviderTask(
        job_id="ff-20261101-abcdef",
        role="builder",
        repo_slug="example/demo",
        worktree_path="worktree",
        branch="forge/ff-20261101-abcdef",
        base_sha="0" * 40,
        plan_path="docs/plan.md",
        plan_sha256=hashlib.sha256(plan.encode("utf-8")).hexdigest(),
        plan_text=plan,
        acceptance_criteria=("hello is printed once",),
        allowed_paths=("*",),
        allowed_operations=("read", "write"),
        validation_commands=(("py", "-m", "unittest"),),
    )
    return replace(task, **fields)  # type: ignore[arg-type]


class BuilderPromptTests(unittest.TestCase):
    def test_a_task_prompt_holds_the_frame_its_task_and_only_titles_of_the_others(self) -> None:
        task = task_for(VALID_PLAN, task_id="T2", done_tasks=("T1",))
        task.validate()
        prompt = prompts.builder_prompt(task)
        self.assertIn(f"sha256 {task.plan_sha256}", prompt)
        for frame in ("## Problem and objective", "Users are not greeted.", "- Print hello.",
                      "## Scope", "Only the greeting."):  # fmt: skip
            self.assertIn(frame, prompt)
        self.assertIn("- T1: Add greet() [done]\n- T2: Document it [current]", prompt)
        self.assertIn("### T2: Document it\n\n- Files: README.md\n", prompt)
        self.assertIn("## Acceptance criteria of T2\n\n- README mentions the greeting\n", prompt)
        for elsewhere in ("Change main.py.", "greet() prints hello", "A greet() function.",
                          "Banner: rejected.", "## Verification"):  # fmt: skip
            self.assertNotIn(elsewhere, prompt)
        pending = prompts.builder_prompt(replace(task, task_id="T1", done_tasks=()))
        self.assertIn("- T1: Add greet() [current]\n- T2: Document it [pending]", pending)
        self.assertIn("Change main.py.", pending)
        # A task without its own criteria falls back to the plan's.
        bare = VALID_PLAN.replace("- [ ] README mentions the greeting", "")
        prompt = prompts.builder_prompt(task_for(bare, task_id="T2"))
        self.assertIn("## Acceptance criteria of T2\n\n- hello is printed once\n", prompt)

    def test_the_plan_text_and_its_hash_check_are_unchanged(self) -> None:
        task = task_for(VALID_PLAN, task_id="T1")
        self.assertEqual(task.plan_text, VALID_PLAN)
        with self.assertRaisesRegex(ValidationError, "not the approved plan"):
            replace(task, plan_text=VALID_PLAN + "\n").validate()
        with self.assertRaisesRegex(ValidationError, "no task T9"):
            replace(task, task_id="T9").validate()

    def test_without_a_task_the_prompt_carries_the_whole_plan(self) -> None:
        prompt = prompts.builder_prompt(task_for(VALID_PLAN))
        self.assertIn(VALID_PLAN.rstrip(), prompt)
        self.assertNotIn("[current]", prompt)


class BuildTaskTests(GitRepoTestCase):
    def test_build_task_carries_the_current_task(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=("x",),
            allowed_paths=("*",),
            task_id="T1",
            done_tasks=(),
        )
        self.assertEqual((task.task_id, task.done_tasks), ("T1", ()))
        self.assertEqual(task.plan_text, self.store.approved_plan_text(job))
