from __future__ import annotations

import os
import unittest

from forgeflow.errors import ActiveJobError, GitHubError, GitHubScopeError, ValidationError
from forgeflow.github import GitHubClient, link_issue
from forgeflow.gitrepo import github_slug_from_remote
from tests.support import FakeGitHub, GitRepoTestCase


class RemoteParsingTests(unittest.TestCase):
    def test_parses_github_remotes(self) -> None:
        for url in (
            "https://github.com/PDJ-E/ForgeFlow.git",
            "https://github.com/PDJ-E/ForgeFlow",
            "git@github.com:PDJ-E/ForgeFlow.git",
            "ssh://git@github.com/PDJ-E/ForgeFlow",
            "https://token@github.com/PDJ-E/ForgeFlow.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(github_slug_from_remote(url), "PDJ-E/ForgeFlow")

    def test_non_github_remote(self) -> None:
        self.assertIsNone(github_slug_from_remote("https://gitlab.com/a/b.git"))


class LinkIssueTests(GitRepoTestCase):
    def test_links_open_issue_and_project_item(self) -> None:
        job = self.approved_job()
        fake = FakeGitHub({7: "OPEN"})
        result = link_issue(self.store, job.job_id, 7, GitHubClient(runner=fake))
        gh = result.job.github
        assert gh.issue is not None
        self.assertEqual((gh.issue.repo, gh.issue.number), ("example/demo", 7))
        self.assertEqual(gh.project_lookup, "ok")
        self.assertEqual(gh.project_items[0].status, "Todo")
        self.assertEqual(result.warnings, ())
        # Read-only: only GraphQL queries were issued, never a mutation.
        self.assertTrue(all("mutation" not in " ".join(c) for c in fake.calls))

    def test_missing_project_scope_is_a_visible_blocker(self) -> None:
        job = self.approved_job()
        fake = FakeGitHub({7: "OPEN"}, project_scope=False)
        result = link_issue(self.store, job.job_id, 7, GitHubClient(runner=fake))
        self.assertEqual(result.job.github.project_lookup, "blocked: token lacks read:project")
        self.assertIn("gh auth refresh -s read:project", result.warnings[0])

    def test_issue_not_on_a_project_is_reported(self) -> None:
        job = self.approved_job()
        fake = FakeGitHub({7: "OPEN"}, project_items=[])
        result = link_issue(self.store, job.job_id, 7, GitHubClient(runner=fake))
        self.assertTrue(result.job.github.project_lookup.startswith("blocked"))

    def test_rejects_closed_issue_and_non_issue(self) -> None:
        job = self.approved_job()
        client = GitHubClient(runner=FakeGitHub({7: "CLOSED"}))
        with self.assertRaises(ValidationError):
            link_issue(self.store, job.job_id, 7, client)
        with self.assertRaises(GitHubError):
            link_issue(self.store, job.job_id, 99, client)
        self.assertIsNone(self.store.load(job.job_id).github.issue)

    def test_one_live_job_per_issue(self) -> None:
        first = self.approved_job()
        client = GitHubClient(runner=FakeGitHub({7: "OPEN"}))
        link_issue(self.store, first.job_id, 7, client)
        second = self.approved_job()
        with self.assertRaisesRegex(ActiveJobError, "already owned"):
            link_issue(self.store, second.job_id, 7, client)

    def test_scope_error_type(self) -> None:
        client = GitHubClient(runner=FakeGitHub({7: "OPEN"}, project_scope=False))
        with self.assertRaises(GitHubScopeError) as ctx:
            client.issue_project_items("example/demo", 7)
        self.assertEqual(ctx.exception.required_scope, "read:project")


@unittest.skipUnless(
    os.environ.get("FORGEFLOW_LIVE_GITHUB") == "1",
    "set FORGEFLOW_LIVE_GITHUB=1 to run read-only checks against github.com",
)
class LiveGitHubTests(unittest.TestCase):
    """REAL GitHub calls (read-only). Target configurable via FORGEFLOW_LIVE_ISSUE=owner/name#N."""

    def target(self) -> tuple[str, int]:
        spec = os.environ.get("FORGEFLOW_LIVE_ISSUE", "cli/cli#2")
        repo, _, number = spec.partition("#")
        return repo, int(number)

    def test_view_issue(self) -> None:
        repo, number = self.target()
        issue = GitHubClient().view_issue(repo, number)
        self.assertEqual(issue.number, number)
        self.assertTrue(issue.url.startswith("https://github.com/"))

    def test_pull_request_number_is_not_accepted_as_issue(self) -> None:
        with self.assertRaises(GitHubError):
            GitHubClient().view_issue("cli/cli", 1)  # cli/cli#1 is a pull request

    def test_view_pull_request(self) -> None:
        # The fields Gate B and record_merge rely on, as GitHub really returns them.
        pr = GitHubClient().view_pull_request("cli/cli", 1)
        self.assertEqual(pr.number, 1)
        self.assertIn(pr.state, ("OPEN", "CLOSED", "MERGED"))
        self.assertEqual(pr.merged, pr.state == "MERGED")
        self.assertRegex(pr.head_sha, r"^[0-9a-f]{40}$")
        self.assertTrue(pr.head_ref and pr.base_ref)

    def test_project_items_or_precise_scope_blocker(self) -> None:
        repo, number = self.target()
        try:
            items = GitHubClient().issue_project_items(repo, number)
        except GitHubScopeError as exc:
            self.assertEqual(exc.required_scope, "read:project")
        else:
            self.assertIsInstance(items, list)


if __name__ == "__main__":
    unittest.main()
