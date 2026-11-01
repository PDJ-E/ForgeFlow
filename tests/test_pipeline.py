"""The Checkpoint C driver: build, checks, review, bounded rework and escalation.

REAL: git, worktrees, ForgeFlow's commits, the check commands, leases, provider subprocesses
(the fake CLI runs as a real child process whose PID the lease records).
MOCKED: the models (tests/helpers/fake_cli.py behind the real Claude/Codex provider classes)
and GitHub (FakeGitHubWorld). Because the providers are simulated, no run here is ever
delivered: every one ends before any push, which the tests assert. Delivery itself is covered
in test_delivery with real commits and checks.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from forgeflow import cli, design, dispatch, pipeline
from forgeflow.codex_runtime import ISOLATION_ARGS
from forgeflow.errors import InvalidTransitionError, ValidationError
from forgeflow.jobs import JobState
from forgeflow.pipeline import MAX_TASK_ATTEMPTS, acceptance_criteria, feedback_for
from forgeflow.proc import run_tracked
from forgeflow.providers import ClaudeCodeProvider, CodexProvider, ProviderUnavailableError
from forgeflow.repoconfig import CONFIG_PATH
from forgeflow.worktree import WORKTREES_RELATIVE
from tests.pipeline_support import APPROVE, CONFIG, TASK_PLAN, PipelineTestCase, request_changes
from tests.support import PROJECT_ROOT, run_git

S = JobState
HELLO = {"writes": {"greet.txt": "hello\n"}, "result": "said hello"}
HI = {"writes": {"greet.txt": "hi\n"}, "result": "said hi"}


class HappyPathTests(PipelineTestCase):
    def test_first_run_preflights_codex_before_worktree_then_builds_and_claude_reviews(
        self,
    ) -> None:
        job = self.handed_off_job()
        target = (self.repo / WORKTREES_RELATIVE / job.job_id).resolve()
        self.assertEqual(job.state, S.PLAN_APPROVED)
        self.assertFalse(target.exists())
        b = self.fake_cli("codex", HELLO)
        r = self.fake_cli("claude", {"review": APPROVE})
        # Keep real preflight/detection; substitute only the CLI, retaining mocked provenance.
        builder = CodexProvider()
        builder.real = False
        made = {"codex": builder, "claude": ClaudeCodeProvider(command=r)}
        starts: list[tuple[list[str], Path, bool]] = []
        popen = subprocess.Popen

        def launch(argv: list[str], **kwargs: Any) -> Any:
            if b[2] in argv:
                starts.append((argv, Path(kwargs["cwd"]).resolve(), target.exists()))
            return popen(argv, **kwargs)

        # Test repositories live in TEMP; model a normal Windows checkout outside TEMP.
        sandbox_temp = self.tmp / "sandbox-temp"
        sandbox_temp.mkdir()
        with (
            mock.patch.object(builder, "_argv0", return_value=[*b, *ISOLATION_ARGS]),
            mock.patch("forgeflow.providers.which", return_value=sys.executable),
            mock.patch("forgeflow.providers.tempfile.gettempdir", return_value=str(sandbox_temp)),
            mock.patch("forgeflow.proc.subprocess.Popen", side_effect=launch),
        ):
            result = self.run_pipeline(
                job.job_id, made.__getitem__, builder="codex", reviewer="claude"
            )
        profile = (self.tmp / "appdata/forgeflow/codex").resolve()
        self.assertEqual(len(starts), 11)  # six detection probes, four verification probes, exec
        self.assertTrue(all(cwd == profile and not exists for _, cwd, exists in starts[:6]))
        self.assertTrue(all(cwd == target and exists for _, cwd, exists in starts[6:]))
        self.assertIn("--version", starts[0][0])
        self.assertIn("login", starts[5][0])
        self.assertIn("exec", starts[-1][0])
        self.assertEqual((target / "greet.txt").read_text(), "hello\n")
        self.assertEqual(
            [(e.kind, e.status) for e in result.evidence],
            [
                ("provider-result", "succeeded"),
                ("build-commit", "committed"),
                ("checks", "passed"),
                ("review", "approve"),
            ],
        )
        self.assertTrue(self.fake_cli_log(r))
        self.assertEqual(result.state, S.NEEDS_HUMAN)  # simulated models never deliver

    def test_codex_invalid_target_fails_preflight_without_changing_approved_job(self) -> None:
        job = self.handed_off_job()
        target = self.repo / WORKTREES_RELATIVE / job.job_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("keep me")
        made = {"codex": CodexProvider(), "claude": ClaudeCodeProvider()}
        with mock.patch("forgeflow.providers.which", return_value=sys.executable):
            with self.assertRaisesRegex(ProviderUnavailableError, "not a directory"):
                self.run_pipeline(job.job_id, made.__getitem__, builder="codex", reviewer="claude")
        self.assertEqual(self.store.load(job.job_id), job)
        self.assertEqual(target.read_text(), "keep me")

    def test_a_simulated_build_runs_every_phase_and_is_never_delivered(self) -> None:
        job = self.handed_off_job()
        factory, builder, reviewer = self.providers(HELLO, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)

        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertEqual((job.policy.builder, job.policy.reviewer), ("claude", "codex"))
        kinds = [(e.kind, e.status, e.mocked) for e in job.evidence]
        self.assertEqual(
            kinds,
            [
                ("provider-result", "succeeded", True),
                ("build-commit", "committed", True),
                ("checks", "passed", False),
                ("review", "approve", True),
            ],
        )
        # ForgeFlow committed the builder's file on the job branch, never on main.
        worktree = self.worktree(job)
        self.assertEqual((worktree / "greet.txt").read_text("utf-8"), "hello\n")
        self.assertEqual((self.repo / "greet.txt").read_text("utf-8"), "todo\n")
        self.assertEqual(run_git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(run_git(worktree, "status", "--porcelain"), "")
        checks = [e for e in job.evidence if e.kind == "checks"][-1]
        self.assertEqual(checks.commit, run_git(worktree, "rev-parse", "HEAD"))
        # Simulated: nothing pushed, no PR.
        self.assertEqual(run_git(self.remote, "for-each-ref"), "")
        self.assertEqual(self.world.pulls, {})
        self.assertIn("simulated build", " ".join(self.lines))
        # The board followed the job through every phase.
        self.assertEqual(self.statuses_set(), ["Ready", "Building", "QA", "Review", "Blocked"])
        self.assertEqual(self.board_status(job), "Blocked")
        # The builder ran in the worktree, the reviewer somewhere else.
        (build_start, *_), (review_start, *_) = (
            self.fake_cli_log(builder),
            self.fake_cli_log(reviewer),
        )
        self.assertEqual(build_start["cwd"], str(worktree))
        self.assertNotEqual(review_start["cwd"], str(worktree))
        self.assertIn("-s", review_start["argv"])
        self.assertEqual(review_start["argv"][review_start["argv"].index("-s") + 1], "read-only")
        self.assertIsNone(job.lease)

    def test_the_issue_is_never_closed(self) -> None:
        job = self.handed_off_job()
        factory, _, _ = self.providers(HELLO, {"review": APPROVE})
        self.run_pipeline(job.job_id, factory)
        self.assertEqual({i["state"] for i in self.world.issues.values()}, {"OPEN"})


class ReworkTests(PipelineTestCase):
    def test_failed_checks_trigger_one_bounded_rework_round(self) -> None:
        job = self.handed_off_job()
        factory, builder, _ = self.providers([HI, HELLO], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual((job.attempts.build, job.attempts.rework), (2, 1))
        checks = [e.status for e in job.evidence if e.kind == "checks"]
        self.assertEqual(checks, ["failed", "passed"])
        prompts = [r["prompt"] for r in self.fake_cli_log(builder) if r["event"] == "start"]
        self.assertNotIn("previous round", prompts[0])
        self.assertIn("Check 'greeting' failed", prompts[1])
        # Each round's checks ran on that round's own commit.
        commits = [e.commit for e in job.evidence if e.kind == "checks"]
        self.assertEqual(len(set(commits)), 2)

    def test_the_rework_limit_escalates_to_a_human(self) -> None:
        job = self.handed_off_job()
        factory, builder, reviewer = self.providers(HI, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertEqual((job.attempts.build, job.attempts.rework), (3, 2))
        self.assertEqual(len([r for r in self.fake_cli_log(builder) if r["event"] == "start"]), 3)
        self.assertEqual(self.fake_cli_log(reviewer), [], "failing builds are not reviewed")
        self.assertIn("checks still fail", " ".join(self.lines))
        self.assertEqual(self.board_status(job), "Blocked")

    def test_review_findings_trigger_rework_but_minor_ones_do_not(self) -> None:
        job = self.handed_off_job()
        reviews = [
            {"review": request_changes("the greeting is missing a newline check")},
            {
                "review": {
                    **request_changes("consider a comma", "minor"),
                    "verdict": "request-changes",
                }
            },
        ]
        factory, builder, reviewer = self.providers([HELLO, HELLO], reviews)
        job = self.run_pipeline(job.job_id, factory)
        statuses = [e.status for e in job.evidence if e.kind == "review"]
        # The second review says "request-changes" but lists only a minor finding: no rework.
        self.assertEqual(statuses, ["request-changes", "approve"])
        self.assertEqual(job.attempts.rework, 1)
        prompts = [r["prompt"] for r in self.fake_cli_log(builder) if r["event"] == "start"]
        self.assertIn("Reviewer finding: [major] greet.txt: the greeting is missing", prompts[1])
        self.assertEqual(len([r for r in self.fake_cli_log(reviewer) if r["event"] == "start"]), 2)

    def test_a_human_can_continue_an_escalated_job_with_a_reason(self) -> None:
        job = self.handed_off_job()
        factory, builder, _ = self.providers([HI, HI, HI, HELLO], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        with self.assertRaisesRegex(ValidationError, "--reason"):
            self.run_pipeline(job.job_id, factory)
        job = self.run_pipeline(job.job_id, factory, reason="the check expects 'hello'")
        self.assertEqual(job.attempts.human_resumes, 1)
        self.assertEqual(job.attempts.build, 4)
        self.assertEqual([e.status for e in job.evidence if e.kind == "checks"][-1], "passed")
        last_prompt = [r["prompt"] for r in self.fake_cli_log(builder) if r["event"] == "start"][-1]
        self.assertIn("the check expects 'hello'", last_prompt)


NOTE = {"writes": {"notes.txt": "it says hello\n"}, "result": "noted"}
BROKEN_NOTE = {"writes": {"notes.txt": "x\n", "greet.txt": "hi\n"}, "result": "broke it"}


class TaskRoundTests(PipelineTestCase):
    """ADR 0006 §2: a round runs, commits and checks the plan's tasks one by one."""

    def starts(self, command: list[str]) -> list[dict[str, Any]]:
        return [r for r in self.fake_cli_log(command) if r["event"] == "start"]

    def subjects(self, job: Any) -> list[str]:
        assert job.worktree is not None
        log = run_git(self.worktree(job), "log", "--format=%s", f"{job.worktree.base_sha}..HEAD")
        return log.splitlines()[::-1]

    def test_each_task_is_built_committed_and_checked_then_the_round_is_verified(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        factory, builder, reviewer = self.providers([HELLO, NOTE], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)

        self.assertEqual(
            [(e.kind, e.status) for e in job.evidence],
            [
                ("task-round", "planned"),
                ("provider-result", "succeeded"),
                ("build-commit", "committed"),
                ("task-result", "passed"),
                ("provider-result", "succeeded"),
                ("build-commit", "committed"),
                ("task-result", "passed"),
                ("checks", "passed"),
                ("review", "approve"),
            ],
        )
        self.assertEqual((job.attempts.build, job.attempts.rework), (1, 0))
        ref = job.ref or job.job_id
        self.assertEqual(self.subjects(job), [f"forgeflow: {ref} T1 round 1",
                                              f"forgeflow: {ref} T2 round 1"])  # fmt: skip
        commits = [e.commit for e in job.evidence if e.kind == "build-commit"]
        results = [e for e in job.evidence if e.kind == "task-result"]
        self.assertEqual([e.commit for e in results], commits)
        payload = self.store.evidence_payload(job, results[1])
        self.assertEqual((payload["task"], payload["attempt"], payload["commit"]),
                         ("T2", 1, commits[1]))  # fmt: skip
        self.assertEqual([c["name"] for c in payload["commands"]], ["greeting"])
        self.assertEqual(payload["builder"]["status"], "succeeded")
        self.assertGreater(payload["builder"]["usage"]["output_tokens"], 0)
        self.assertTrue(results[1].mocked, "the builder was simulated")
        # The final checks and the review ran on the last task's commit.
        self.assertEqual([e.commit for e in job.evidence if e.kind in ("checks", "review")],
                         [commits[1]] * 2)  # fmt: skip
        first, second = (s["prompt"] for s in self.starts(builder))
        self.assertIn("- T1: Write the greeting [current]\n- T2: Add a note [pending]", first)
        self.assertIn("- T1: Write the greeting [done]\n- T2: Add a note [current]", second)
        self.assertNotIn("notes.txt explains", first)
        self.assertEqual(len(self.starts(reviewer)), 1)
        self.assertEqual(job.state, S.NEEDS_HUMAN)  # simulated: never delivered
        self.assertEqual(self.world.pulls, {})

    def test_a_task_whose_checks_fail_is_retried_with_the_failures(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        factory, builder, _ = self.providers([HELLO, BROKEN_NOTE, HELLO], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        results = [e for e in job.evidence if e.kind == "task-result"]
        self.assertEqual([e.status for e in results], ["passed", "failed", "passed"])
        payloads = [self.store.evidence_payload(job, e) for e in results]
        self.assertEqual([(p["task"], p["attempt"]) for p in payloads],
                         [("T1", 1), ("T2", 1), ("T2", 2)])  # fmt: skip
        retry = self.starts(builder)[2]["prompt"]
        self.assertIn("Add a note [current]", retry)
        self.assertIn("Check 'greeting' failed", retry)
        self.assertEqual((job.attempts.build, job.attempts.rework), (1, 0))
        self.assertEqual([e.status for e in job.evidence if e.kind == "review"], ["approve"])

    def test_past_the_task_limit_a_human_gets_the_task_and_earlier_commits_stay(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        factory, builder, reviewer = self.providers([HELLO, BROKEN_NOTE], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertEqual(len(self.starts(builder)), 1 + MAX_TASK_ATTEMPTS)
        statuses = [e.status for e in job.evidence if e.kind == "task-result"]
        self.assertEqual(statuses, ["passed"] + ["failed"] * MAX_TASK_ATTEMPTS)
        self.assertIn(f"task T2 (Add a note) still fails its checks after {MAX_TASK_ATTEMPTS}",
                      " ".join(self.lines))  # fmt: skip
        ref = job.ref or job.job_id
        self.assertEqual(self.subjects(job)[0], f"forgeflow: {ref} T1 round 1")
        # Retries that change nothing more add no commit; the first T2 attempt is kept.
        self.assertEqual(self.subjects(job)[1:], [f"forgeflow: {ref} T2 round 1"])
        self.assertNotIn("checks", [e.kind for e in job.evidence])
        self.assertEqual(self.starts(reviewer), [])
        self.assertEqual(job.attempts.build, 1)

    def rework_once(self, finding: dict[str, Any]) -> tuple[Any, list[str]]:
        """Round 1 builds T1, T2; the review asks for ``finding``; round 2 is approved."""
        job = self.handed_off_job(TASK_PLAN)
        reviews = [{"review": {**request_changes("x"), "findings": [finding]}},
                   {"review": APPROVE}]  # fmt: skip
        factory, builder, _ = self.providers([HELLO, NOTE], reviews)
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual([e.status for e in job.evidence if e.kind == "review"],
                         ["request-changes", "approve"])  # fmt: skip
        self.assertEqual((job.attempts.build, job.attempts.rework), (2, 1))
        return job, [s["prompt"] for s in self.starts(builder)]

    def assert_rebuilt_only(self, finding: dict[str, Any], rebuilt: str) -> None:
        job, prompts = self.rework_once(finding)
        self.assertEqual(len(prompts), 3)
        current = [line for line in prompts[2].splitlines() if line.endswith("[current]")]
        self.assertEqual(len(current), 1)
        self.assertTrue(current[0].startswith(f"- {rebuilt}: "))
        self.assertIn("Reviewer finding: [major]", prompts[2])
        rounds = [e for e in job.evidence if e.kind == "task-round"]
        self.assertEqual(self.store.evidence_payload(job, rounds[-1])["tasks"], [rebuilt])

    def test_rework_rebuilds_only_the_task_whose_file_a_finding_names(self) -> None:
        finding = {"severity": "major", "file": "greet.txt", "issue": "too terse",
                   "suggestion": "say more"}  # fmt: skip
        self.assert_rebuilt_only(finding, "T1")

    def test_rework_rebuilds_only_the_task_a_finding_names_by_id(self) -> None:
        finding = {"severity": "major", "file": "-", "issue": "T2 must mention the greeting",
                   "suggestion": "add it"}  # fmt: skip
        self.assert_rebuilt_only(finding, "T2")

    def test_a_finding_that_names_no_task_rebuilds_every_task(self) -> None:
        finding = {"severity": "major", "file": "README.md", "issue": "unrelated",
                   "suggestion": "fix"}  # fmt: skip
        _, prompts = self.rework_once(finding)
        self.assertEqual(len(prompts), 4)

    def test_a_rework_round_stopped_as_it_starts_still_rebuilds_its_tasks(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        finding = {"severity": "major", "file": "greet.txt", "issue": "too terse",
                   "suggestion": "say more"}  # fmt: skip
        reviews = [{"review": {**request_changes("x"), "findings": [finding]}},
                   {"review": APPROVE}]  # fmt: skip
        factory, builder, _ = self.providers([HELLO, NOTE, HELLO], reviews)
        real_start = dispatch.start_build
        calls: list[int] = []

        def start_then_die(*args: Any, **kwargs: Any) -> None:
            real_start(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:  # the rework round: BUILDING entered, nothing built yet
                raise RuntimeError("orchestrator killed")

        with mock.patch("forgeflow.pipeline.start_build", side_effect=start_then_die):
            with self.assertRaisesRegex(RuntimeError, "killed"):
                self.run_pipeline(job.job_id, factory)
        self.assertEqual(self.store.load(job.job_id).state, S.INTERRUPTED)
        job = self.run_pipeline(job.job_id, factory)
        prompts = [s["prompt"] for s in self.starts(builder)]
        self.assertEqual(len(prompts), 3, "the rework's task is built after the resume")
        self.assertIn("- T1: Write the greeting [current]", prompts[2])
        self.assertEqual([e.status for e in job.evidence if e.kind == "review"],
                         ["request-changes", "approve"])  # fmt: skip

    def test_a_human_go_ahead_after_the_task_limit_restarts_at_that_task(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        steps = [HELLO, *[BROKEN_NOTE] * MAX_TASK_ATTEMPTS, {**NOTE, "writes": {
            "notes.txt": "it says hello\n", "greet.txt": "hello\n"}}]  # fmt: skip
        factory, builder, _ = self.providers(steps, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        job = self.run_pipeline(job.job_id, factory, reason="the note must keep the greeting")
        prompts = [s["prompt"] for s in self.starts(builder)]
        self.assertEqual(len(prompts), 2 + MAX_TASK_ATTEMPTS, "T1 is not built again")
        self.assertIn("- T1: Write the greeting [done]\n- T2: Add a note [current]", prompts[-1])
        self.assertIn("the note must keep the greeting", prompts[-1])
        self.assertEqual([e.status for e in job.evidence if e.kind == "review"], ["approve"])

    def test_an_interrupted_round_whose_tasks_all_passed_builds_nothing(self) -> None:
        job = self.handed_off_job(TASK_PLAN)
        factory, _, _ = self.providers([HELLO, NOTE], {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        parts = design.plan_parts(TASK_PLAN)
        interrupted = replace(job, state=S.INTERRUPTED, interrupted_from=S.BUILDING)
        self.assertEqual(pipeline.round_tasks(self.store, interrupted, parts), [])
        ready = replace(job, state=S.READY)  # a human's go-ahead: rework by the findings
        self.assertEqual(pipeline.round_tasks(self.store, ready, parts), list(parts.tasks))
        # A round that was planned but never started (BUILDING refused) does not count.
        self.store.record_evidence(job.job_id, "task-round", "planned",
                                   {"round": 2, "tasks": ["T1"], "done": ["T2"]},
                                   mocked=False)  # fmt: skip
        ready = replace(self.store.load(job.job_id), state=S.READY)
        self.assertEqual(pipeline.round_tasks(self.store, ready, parts), list(parts.tasks))

    def test_a_plan_without_tasks_is_built_in_one_run_with_a_warning(self) -> None:
        job = self.handed_off_job()
        factory, builder, _ = self.providers(HELLO, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertIn("the plan has no '### T<n>: <title>' tasks", " ".join(self.lines))
        self.assertNotIn("task-result", [e.kind for e in job.evidence])
        self.assertNotIn("[current]", self.starts(builder)[0]["prompt"])

    def test_task_results_come_only_from_checks_run_in_a_build_round(self) -> None:
        job = self.ready_job()
        with self.assertRaisesRegex(ValidationError, "recorded only by"):
            self.store.record_evidence(job.job_id, "task-result", "passed", {}, mocked=False)
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaisesRegex(InvalidTransitionError, "run in building"):
                self.store.run_task_checks(
                    job.job_id, lease.token, [["git", "status"]], task="T1", details={},
                    mocked=False,
                )  # fmt: skip


class SafetyTests(PipelineTestCase):
    def test_out_of_scope_changes_are_kept_uncommitted_and_escalated(self) -> None:
        job = self.handed_off_job()
        rogue = {"writes": {"greet.txt": "hello\n", ".forge/forgeflow.toml": "[checks]\n"}}
        factory, _, reviewer = self.providers(rogue, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        worktree = self.worktree(job)
        assert job.worktree is not None
        self.assertEqual(run_git(worktree, "rev-parse", "HEAD"), job.worktree.base_sha)
        status = run_git(worktree, "status", "--porcelain")
        self.assertIn(".forge/forgeflow.toml", status)  # left exactly where it was
        self.assertEqual((worktree / ".forge" / "forgeflow.toml").read_text("utf-8"), "[checks]\n")
        self.assertIn("scope-violation", [e.kind for e in job.evidence])
        self.assertEqual(self.fake_cli_log(reviewer), [])

    def test_this_repositorys_agent_files_are_protected_from_the_builder(self) -> None:
        # ForgeFlow's own committed configuration, applied to the test repository.
        build = tomllib.loads((PROJECT_ROOT / CONFIG_PATH).read_text("utf-8"))["build"]
        config = (
            f"{CONFIG}\n[build]\nallowed_paths = {json.dumps(build['allowed_paths'])}\n"
            f"protected_paths = {json.dumps(build['protected_paths'])}\n"
        )
        (self.repo / CONFIG_PATH).write_text(config, encoding="utf-8")
        run_git(self.repo, "commit", "-q", "-am", "protect agent files")
        run_git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        job = self.handed_off_job()
        planted = [".claude/settings.json", ".claude/hooks/x.sh", ".agents/a.md", "CLAUDE.md",
                   "AGENTS.md"]  # fmt: skip
        rogue = {"writes": {"greet.txt": "hello\n", **dict.fromkeys(planted, "x\n")}}
        factory, _, reviewer = self.providers(rogue, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        [violation] = [e for e in job.evidence if e.kind == "scope-violation"]
        payload = self.store.evidence_payload(job, violation)
        self.assertEqual(sorted(payload["protected"]), sorted(planted))
        self.assertEqual(self.fake_cli_log(reviewer), [])

    def run_with_budgets(self, limited: str = "builder", **options: Any) -> tuple[Any, float]:
        """Claude as ``limited`` (builder or reviewer) stopped by its budget under configured
        limits, Codex in the other role. Returns the job and Claude's process timeout."""
        budgets = "\n[build]\nmax_turns = 7\nmax_budget_usd = 1.5\ntask_timeout_seconds = 120\n"
        (self.repo / CONFIG_PATH).write_text(CONFIG + budgets, encoding="utf-8")
        run_git(self.repo, "commit", "-q", "-am", "budgets")
        run_git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        timeouts: list[float] = []

        def runner(argv: Any, **kwargs: Any) -> Any:
            timeouts.append(kwargs["timeout"])
            return run_tracked(argv, **kwargs)

        job = self.handed_off_job()
        limit = {"limit": "max_budget_usd", "exit": 1}
        c = self.fake_cli("claude", {**HELLO, **limit} if limited == "builder" else limit)
        x = self.fake_cli("codex", {"review": APPROVE} if limited == "builder" else HELLO)
        claude = ClaudeCodeProvider(command=c, runner=runner)
        claude.features = {"--max-turns": True, "--max-budget-usd": True}  # fakes skip detect
        made = {"claude": claude, "codex": CodexProvider(command=x)}
        other = "codex" if limited == "builder" else "claude"
        roles = {"builder": "claude", "reviewer": other} if limited == "builder" else {
            "builder": "codex", "reviewer": "claude"}  # fmt: skip
        job = self.run_pipeline(job.job_id, made.__getitem__, **roles, **options)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        [start] = [e for e in self.fake_cli_log(c) if e["event"] == "start"]
        argv = start["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.5")
        self.assertEqual(job.attempts.build, 1, "a limit is not retried")
        self.assertIn("budget_exhausted", " ".join(self.lines))
        self.assertEqual(self.world.pulls, {})
        [timeout] = timeouts
        return job, timeout

    def test_configured_budgets_reach_the_builder_and_a_limit_escalates_once(self) -> None:
        job, timeout = self.run_with_budgets()
        self.assertEqual(timeout, 120)
        self.assertEqual(
            [e.status for e in job.evidence if e.kind == "provider-result"], ["budget_exhausted"]
        )
        self.assertNotIn("review", [e.kind for e in job.evidence])

    def test_a_reviewer_gets_the_same_budgets_and_a_limit_escalates_once(self) -> None:
        job, timeout = self.run_with_budgets("reviewer")
        self.assertEqual(timeout, 120)
        self.assertEqual(
            [e.status for e in job.evidence if e.kind == "review"], ["budget_exhausted"]
        )

    def test_an_explicit_timeout_overrides_the_configured_one(self) -> None:
        self.assertEqual(self.run_with_budgets(timeout_seconds=45)[1], 45)
        for command in ("run", "resume"):
            with self.subTest(command):
                self.assertIsNone(cli._parser().parse_args([command, "J1"]).timeout)

    def test_a_zero_timeout_is_refused_not_replaced(self) -> None:
        job = self.handed_off_job()
        factory, builder, _ = self.providers(HELLO, {"review": APPROVE})
        with self.assertRaisesRegex(ValidationError, "timeout"):
            self.run_pipeline(job.job_id, factory, timeout_seconds=0)
        self.assertEqual(self.fake_cli_log(builder), [])

    def test_an_unavailable_reviewer_blocks_before_anything_is_dispatched(self) -> None:
        job = self.handed_off_job()
        builder = self.fake_cli("claude", HELLO)
        made = {"claude": ClaudeCodeProvider(command=builder), "codex": CodexProvider()}
        with mock.patch("forgeflow.providers.which", return_value=None):
            with self.assertRaisesRegex(ProviderUnavailableError, "codex cannot run as reviewer"):
                self.run_pipeline(job.job_id, made.__getitem__)
        job = self.store.load(job.job_id)
        self.assertEqual(job.state, S.PLAN_APPROVED)
        self.assertIsNone(job.worktree)
        self.assertEqual(self.fake_cli_log(builder), [])

    def test_a_failing_reviewer_never_counts_as_an_approval(self) -> None:
        job = self.handed_off_job()
        factory, _, _ = self.providers(HELLO, {"exit": 1, "stderr": "auth expired"})
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertNotIn("approve", [e.status for e in job.evidence if e.kind == "review"])
        self.assertIn("Nothing was approved", " ".join(self.lines))

    def test_an_approval_without_snapshot_access_is_not_an_approval(self) -> None:
        """Regression (live run 2026-10-26): Codex returned valid "approve" JSON although it
        could not read the snapshot. Such a review is blocked: no approval, no rework."""
        job = self.handed_off_job()
        cases = [
            {"review": APPROVE, "cannot_read": True},  # no token: never read the snapshot
            {"review": {**APPROVE, "inspection_complete": False}},  # says it could not inspect
        ]
        for step in cases:
            with self.subTest(step):
                job = self.handed_off_job()
                factory, builder, reviewer = self.providers(HELLO, step)
                job = self.run_pipeline(job.job_id, factory)
                self.assertEqual(job.state, S.NEEDS_HUMAN)
                reviews = [e for e in job.evidence if e.kind == "review"]
                self.assertEqual([r.status for r in reviews], ["blocked"])
                self.assertEqual(job.attempts.rework, 0, "an infrastructure problem is not rework")
                builds = [r for r in self.fake_cli_log(builder) if r["event"] == "start"]
                self.assertEqual(len(builds), 1)
                self.assertEqual(self.world.pulls, {})
                self.assertIn("Nothing was approved", " ".join(self.lines))
                prompt = self.fake_cli_log(reviewer)[0]["prompt"]
                self.assertIn(".forgeflow-review-token", prompt)
                self.store.transition(job.job_id, S.CANCELLED, reason="next case")

    def test_the_token_never_appears_in_the_prompt(self) -> None:
        job = self.handed_off_job()
        factory, _, reviewer = self.providers(HELLO, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        review = [e for e in job.evidence if e.kind == "review"][-1]
        self.assertEqual(review.status, "approve")
        payload = json.loads((self.store.job_dir(job.job_id) / review.path).read_text("utf-8"))
        token = payload["payload"]["review"]["snapshot_token"]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertNotIn(token, self.fake_cli_log(reviewer)[0]["prompt"])

    def test_the_reviewer_cannot_touch_the_build(self) -> None:
        job = self.handed_off_job()
        sneaky = {"review": APPROVE, "writes": {"greet.txt": "hijacked\n"}}
        factory, _, reviewer = self.providers(HELLO, sneaky)
        job = self.run_pipeline(job.job_id, factory)
        worktree = self.worktree(job)
        self.assertEqual((worktree / "greet.txt").read_text("utf-8"), "hello\n")
        self.assertEqual(run_git(worktree, "status", "--porcelain"), "")
        snapshot = self.fake_cli_log(reviewer)[0]["cwd"]
        self.assertNotIn(str(self.repo), snapshot)

    def test_codex_sandbox_refusal_is_a_blocker_not_a_fallback(self) -> None:
        job = self.handed_off_job()
        refused = self.fake_cli("codex", {"sandbox_refusal": True})
        made = {"codex": CodexProvider(command=refused), "claude": ClaudeCodeProvider()}
        with mock.patch.object(ClaudeCodeProvider, "preflight"):
            job = self.run_pipeline(
                job.job_id, made.__getitem__, builder="codex", reviewer="claude"
            )
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertEqual([e.status for e in job.evidence][:1], ["blocked"])
        self.assertIn("sandbox", " ".join(self.lines))

    def test_gate_a_and_the_handoff_come_first(self) -> None:
        factory, builder, _ = self.providers(HELLO, {"review": APPROVE})
        draft = self.new_job()
        with self.assertRaisesRegex(InvalidTransitionError, "Gate A"):
            self.run_pipeline(draft.job_id, factory)
        approved = self.approved_job()
        with self.assertRaisesRegex(InvalidTransitionError, "handoff"):
            self.run_pipeline(approved.job_id, factory)
        self.assertEqual(self.fake_cli_log(builder), [])

    def test_providers_must_be_chosen_explicitly(self) -> None:
        job = self.handed_off_job()
        factory, _, _ = self.providers(HELLO, {"review": APPROVE})
        with self.assertRaisesRegex(ValidationError, "explicitly"):
            self.run_pipeline(job.job_id, factory, builder=None, reviewer=None)

    def test_no_checks_configured_is_refused(self) -> None:
        run_git(self.repo, "rm", "-q", ".forge/forgeflow.toml")
        run_git(self.repo, "commit", "-q", "-m", "drop config")
        run_git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        job = self.handed_off_job()
        factory, builder, _ = self.providers(HELLO, {"review": APPROVE})
        with self.assertRaisesRegex(ValidationError, "does not build without"):
            self.run_pipeline(job.job_id, factory)
        self.assertEqual(self.fake_cli_log(builder), [])

    def test_a_job_at_gate_b_is_not_run_again(self) -> None:
        job = self.handed_off_job()
        self.tamper(job.job_id, lambda d: d.update(state="awaiting_merge_approval"))
        factory, _, _ = self.providers(HELLO, {"review": APPROVE})
        with self.assertRaisesRegex(InvalidTransitionError, "Gate B"):
            self.run_pipeline(job.job_id, factory)


class HelperTests(PipelineTestCase):
    def test_acceptance_criteria_are_read_from_the_plan(self) -> None:
        plan = "# T\n\n## Acceptance criteria\n\n- [ ] one\n- [x] two\n- [ ] one\n"
        self.assertEqual(acceptance_criteria(plan, "fallback"), ("one", "two"))
        self.assertEqual(acceptance_criteria("# T\n", "fallback"), ("fallback",))

    def test_the_review_snapshot_is_exactly_the_commit(self) -> None:
        from forgeflow.dispatch import make_snapshot
        from tests.support import rmtree

        snapshot = make_snapshot(self.repo, "HEAD")
        self.addCleanup(rmtree, snapshot)
        files = sorted(
            p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()
        )
        self.assertEqual(files, [".forge/forgeflow.toml", "README.md", "docs/plan.md", "greet.txt"])

    def test_only_the_jobs_own_stale_snapshots_are_removed(self) -> None:
        from forgeflow.dispatch import make_snapshot, remove_stale_snapshots
        from tests.support import rmtree

        mine = make_snapshot(self.repo, "HEAD", "ff-20261026-aaaaaa")
        other = make_snapshot(self.repo, "HEAD", "ff-20261026-bbbbbb")
        self.addCleanup(rmtree, other)
        remove_stale_snapshots("ff-20261026-aaaaaa")
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL inheritance")
    def test_the_review_snapshot_keeps_inherited_permissions(self) -> None:
        """Regression (live run 2026-10-26): a mkdtemp directory has an owner-only ACL on
        Windows, so Codex's sandbox identity got "Access is denied" reading the snapshot.

        The snapshot must get exactly the permissions of an ordinary new directory in the same
        place (what that is depends on the machine; on the pilot it includes CodexSandboxUsers).
        """
        import tempfile

        from forgeflow.dispatch import make_snapshot
        from tests.support import rmtree

        def acl(path: Path) -> list[str]:
            out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
            lines = [line.strip() for line in out.splitlines() if ":(" in line]
            return sorted(line.removeprefix(str(path)).strip() for line in lines)

        snapshot = make_snapshot(self.repo, "HEAD")
        self.addCleanup(rmtree, snapshot)
        ordinary = Path(tempfile.gettempdir()) / f"{snapshot.name}-ordinary"
        ordinary.mkdir()
        self.addCleanup(rmtree, ordinary)
        self.assertTrue(acl(snapshot))
        self.assertEqual(acl(snapshot), acl(ordinary))

    def test_feedback_is_rebuilt_from_the_record(self) -> None:
        job = self.handed_off_job()
        factory, _, _ = self.providers(HI, {"review": APPROVE})
        job = self.run_pipeline(job.job_id, factory)
        feedback = feedback_for(self.store, job)
        self.assertTrue(any("Check 'greeting' failed" in f for f in feedback))


if __name__ == "__main__":
    unittest.main()
