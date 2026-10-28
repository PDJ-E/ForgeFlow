from __future__ import annotations

import os
import unittest

from forgeflow.errors import GitHubError, GitHubScopeError
from forgeflow.github import GitHubClient
from forgeflow.gitrepo import github_slug_from_remote
from tests.support import FakeGitHub


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


class IssueClientTests(unittest.TestCase):
    def test_open_issue_and_project_item(self) -> None:
        fake = FakeGitHub({7: "OPEN"})
        client = GitHubClient(runner=fake)
        issue = client.view_issue("example/demo", 7)
        self.assertEqual((issue.repo, issue.number, issue.state), ("example/demo", 7, "OPEN"))
        self.assertEqual(client.issue_project_items("example/demo", 7)[0].status, "Todo")
        self.assertTrue(all("mutation" not in " ".join(c) for c in fake.calls))

    def test_closed_issue(self) -> None:
        client = GitHubClient(runner=FakeGitHub({7: "CLOSED"}))
        self.assertEqual(client.view_issue("example/demo", 7).state, "CLOSED")

    def test_missing_issue(self) -> None:
        with self.assertRaises(GitHubError):
            GitHubClient(runner=FakeGitHub({})).view_issue("example/demo", 99)

    def test_no_project(self) -> None:
        client = GitHubClient(runner=FakeGitHub({7: "OPEN"}, project_items=[]))
        self.assertEqual(client.issue_project_items("example/demo", 7), [])

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
