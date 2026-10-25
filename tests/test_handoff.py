"""GitHub handoff of an approved plan, against a stateful fake `gh` (no network, MOCKED).

Real GitHub behaviour is exercised separately by the opt-in LiveProjectReadTests below
(read-only) and by the supervised end-to-end handoff recorded in the PR.
"""

from __future__ import annotations

import os
import unittest

from forgeflow import discovery
from forgeflow.config import ProjectBinding, bind_project, load_binding
from forgeflow.errors import (
    GitHubError,
    GitHubScopeError,
    InvalidTransitionError,
    LeaseError,
    ValidationError,
)
from forgeflow.github import GitHubClient
from forgeflow.handoff import handoff, marker_for
from forgeflow.jobs import UNASSIGNED_PROVIDER, Job, JobState
from tests.fake_github import PROJECT_URL, FakeGitHubWorld, SimulatedCrash
from tests.support import GitRepoTestCase

BINDING = ProjectBinding(
    owner="example",
    number=2,
    project_id="PVT_project",
    title="ForgeFlow Development",
    url=PROJECT_URL,
    ready_status="Ready",
    bound_at="2026-10-25T00:00:00Z",
)


class HandoffTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.world = FakeGitHubWorld()
        self.client = GitHubClient(runner=self.world)

    def approved(self) -> Job:
        job, _ = discovery.start(self.store, "Greeting", "greet", self.repo_record())
        discovery.add(
            self.store, job.job_id, "problem", "PRIVATE-DISCOVERY-NOTE", status="accepted"
        )
        self.store.submit_plan(job.job_id, "docs/plan.md")
        return self.approve(job.job_id)

    def run_handoff(self, job: Job, **kwargs: int) -> Job:
        return handoff(self.store, job.job_id, self.client, BINDING, **kwargs).job

    def test_creates_one_issue_on_the_project_in_ready(self) -> None:
        job = self.approved()
        result = handoff(self.store, job.job_id, self.client, BINDING)
        self.assertEqual(self.world.writes, ["create_issue", "add_item", "set_status"])
        issue = self.world.issues[1]
        snapshot = self.store.approved_plan_text(job)
        self.assertTrue(issue["body"].startswith(snapshot.rstrip()))  # the approved bytes
        assert job.plan.approval is not None
        self.assertIn(marker_for(job.job_id, job.plan.approval.subject_sha256), issue["body"])
        self.assertEqual(issue["title"], "Plan")  # the plan's '# Title'
        self.assertNotIn("PRIVATE-DISCOVERY-NOTE", issue["body"])
        item = self.world.item_for(1)
        assert item is not None
        self.assertEqual(item["status"], "Ready")

        linked = result.job
        assert linked.github.issue is not None and linked.github.handoff is not None
        self.assertEqual(linked.github.issue.number, 1)
        self.assertEqual(linked.github.project_items[0].status, "Ready")
        self.assertIsNotNone(linked.github.handoff.completed_at)
        self.assertIs(linked.state, JobState.PLAN_APPROVED)  # no worktree, nothing dispatched
        self.assertIsNone(linked.worktree)

    def test_footer_links_the_project_by_url_not_by_number(self) -> None:
        self.run_handoff(self.approved())
        body = self.world.issues[1]["body"]
        footer = body.split("\n---\n")[-1]
        self.assertIn(f"[ForgeFlow Development]({PROJECT_URL})", footer)
        self.assertNotRegex(footer, r"#\d")  # nothing GitHub would auto-link to an issue/PR

    def test_rerun_is_idempotent(self) -> None:
        job = self.approved()
        self.run_handoff(job)
        writes = list(self.world.writes)
        result = handoff(self.store, job.job_id, self.client, BINDING)
        self.assertEqual(self.world.writes, writes)  # no new issue, item or status write
        self.assertEqual(result.actions, [])
        self.assertEqual(len(self.world.issues), 1)
        self.assertEqual(len(self.world.items), 1)

    def test_interrupted_after_creating_the_issue_does_not_duplicate_it(self) -> None:
        job = self.approved()
        self.world.crash_after.add("create_issue")
        with self.assertRaises(SimulatedCrash):
            self.run_handoff(job)
        recorded = self.store.load(job.job_id)
        self.assertIsNone(recorded.github.issue)  # the crash hit before the link was saved
        self.assertIsNotNone(recorded.github.handoff)  # but the intent and marker were
        result = handoff(self.store, job.job_id, self.client, BINDING)
        self.assertEqual(len(self.world.issues), 1)
        self.assertIn("found issue #1 from an interrupted run", result.actions)
        self.assertEqual(self.world.writes.count("create_issue"), 1)

    def test_interrupted_after_adding_the_item_does_not_duplicate_it(self) -> None:
        job = self.approved()
        self.world.crash_after.add("add_item")
        with self.assertRaises(SimulatedCrash):
            self.run_handoff(job)
        self.run_handoff(job)
        self.assertEqual(self.world.writes.count("add_item"), 1)
        self.assertEqual(len(self.world.items), 1)
        item = self.world.item_for(1)
        assert item is not None
        self.assertEqual(item["status"], "Ready")

    def test_status_set_on_the_board_is_not_overridden(self) -> None:
        job = self.approved()
        self.run_handoff(job)
        item = self.world.item_for(1)
        assert item is not None
        item["status"] = "Building"  # a human moved the card
        result = handoff(self.store, job.job_id, self.client, BINDING)
        self.assertEqual(item["status"], "Building")
        self.assertTrue(any("left it unchanged" in w for w in result.warnings))

    def test_unapproved_or_changed_plans_publish_nothing(self) -> None:
        draft, _ = discovery.start(self.store, "Draft", "d", self.repo_record())
        awaiting = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        for job in (draft, awaiting):
            with self.subTest(job.state.value), self.assertRaises(InvalidTransitionError):
                self.run_handoff(job)
        approved = self.approved()
        (self.repo / "docs" / "plan.md").write_text("# Plan\n\nChanged.\n", encoding="utf-8")
        with self.assertRaisesRegex(InvalidTransitionError, "changed after approval"):
            self.run_handoff(approved)
        self.assertEqual(self.world.writes, [])

    def test_missing_ready_option_or_status_field_is_a_clear_error(self) -> None:
        job = self.approved()
        for statuses, expected in (
            (("Todo", "Done"), "no Status option 'Ready'"),
            (None, "no single-select"),
        ):
            with self.subTest(expected):
                self.world.statuses = statuses
                with self.assertRaisesRegex(GitHubError, expected):
                    self.run_handoff(job)
        self.assertEqual(self.world.writes, [])

    def test_missing_project_scope_is_reported_precisely(self) -> None:
        job = self.approved()
        self.world.mutation_scope = False
        with self.assertRaises(GitHubScopeError) as ctx:
            self.run_handoff(job)
        self.assertEqual(ctx.exception.required_scope, "project")
        self.assertIn("gh auth refresh -s project", str(ctx.exception))
        # The issue exists; the retry after fixing the scope only finishes the Project part.
        self.world.mutation_scope = True
        self.run_handoff(job)
        self.assertEqual(self.world.writes.count("create_issue"), 1)

    def test_github_unreachable_is_a_clear_error_and_retryable(self) -> None:
        job = self.approved()
        self.world.offline = True
        with self.assertRaisesRegex(GitHubError, "unreachable"):
            self.run_handoff(job)
        self.world.offline = False
        self.run_handoff(job)
        self.assertEqual(len(self.world.issues), 1)

    def test_no_project_configured(self) -> None:
        job = self.approved()
        with self.assertRaisesRegex(ValidationError, "forgeflow init --project"):
            handoff(self.store, job.job_id, self.client, None)

    def test_link_existing_issue_comments_once(self) -> None:
        self.world.seed_issue(9, body="written by the owner")
        job = self.approved()
        self.run_handoff(job, link_number=9)
        self.run_handoff(job, link_number=9)
        self.assertEqual(len(self.world.issues[9]["comments"]), 1)
        self.assertEqual(self.world.issues[9]["body"], "written by the owner")  # not edited
        self.assertNotIn("create_issue", self.world.writes)
        with self.assertRaisesRegex(ValidationError, "already linked to issue #9"):
            self.run_handoff(job, link_number=10)

    def test_link_refuses_a_closed_issue(self) -> None:
        self.world.seed_issue(4, state="CLOSED")
        job = self.approved()
        with self.assertRaisesRegex(ValidationError, "CLOSED"):
            self.run_handoff(job, link_number=4)
        self.assertEqual(self.world.writes, [])

    def test_handed_off_job_cannot_dispatch_a_builder(self) -> None:
        job = self.run_handoff(self.approved())
        self.assertEqual(job.policy.builder, UNASSIGNED_PROVIDER)
        with self.assertRaises(LeaseError):
            self.store.acquire_lease(job.job_id, "builder", "claude")
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, JobState.READY)  # needs a worktree first
        self.assertFalse((self.repo / ".forge" / "worktrees").exists())


class ProjectBindingTests(GitRepoTestCase):
    def test_bind_records_existing_option_and_never_writes_github(self) -> None:
        world = FakeGitHubWorld()
        binding = bind_project(self.repo, GitHubClient(runner=world), "example", 2, "ready")
        self.assertEqual((binding.project_id, binding.ready_status), ("PVT_project", "Ready"))
        self.assertEqual(load_binding(self.repo), binding)
        self.assertEqual(world.writes, [])

    def test_bind_refuses_unknown_option(self) -> None:
        world = FakeGitHubWorld(statuses=("Todo", "Done"))
        with self.assertRaisesRegex(GitHubError, "options: Todo, Done"):
            bind_project(self.repo, GitHubClient(runner=world), "example", 2)
        self.assertIsNone(load_binding(self.repo))


@unittest.skipUnless(
    os.environ.get("FORGEFLOW_LIVE_GITHUB") == "1",
    "set FORGEFLOW_LIVE_GITHUB=1 to read a real GitHub Project (read-only)",
)
class LiveProjectReadTests(unittest.TestCase):
    """REAL, read-only: the Project's Status field as GitHub reports it.

    Target: FORGEFLOW_LIVE_PROJECT=owner#number (default PDJ-E#2).
    """

    def test_project_status_field(self) -> None:
        owner, _, number = os.environ.get("FORGEFLOW_LIVE_PROJECT", "PDJ-E#2").partition("#")
        project = GitHubClient().project_info(owner, int(number))
        self.assertTrue(project.id.startswith("PVT_"))
        self.assertIsNotNone(project.status_field_id)
        self.assertTrue(project.status_options)

    def test_repository_info(self) -> None:
        info = GitHubClient().repository_info("cli/cli")
        self.assertFalse(info.is_private)
        self.assertEqual(info.visibility, "PUBLIC")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
