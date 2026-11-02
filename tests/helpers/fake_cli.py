"""Stand-in for the `claude` and `codex` CLIs in tests. No model is ever called.

Usage (as a provider ``command``): python fake_cli.py <claude|codex> <script.json> <cli args...>

It accepts the arguments ForgeFlow passes to the real CLI, reads the prompt from stdin like
the real one, and prints output in the format each real CLI was observed to print (Claude
``--output-format stream-json --verbose``: a ``system/init`` event; per write, one
``assistant`` message sent as a thinking and a tool_use event that share its id and usage,
flushed before any ``sleep``; the answer and a ``result`` event. Codex: JSONL events plus the
``-o`` last-message file).

``script.json`` holds one step (an object) or a list of steps, one per invocation; a step is
consumed when used, and the last one repeats. A step may contain:

* ``writes``: {relative path: content} written in the working directory (Codex: ``-C``);
* ``result``: the builder's final message; ``review``: the reviewer's structured answer;
* ``sleep``: seconds to wait after writing, before answering (interruption tests);
* ``exit``, ``stderr``, ``is_error``: failure modes; ``sandbox_refusal``: Codex's message;
* ``limit``: Claude stopped by ``max_turns`` or ``max_budget_usd`` (use with ``exit: 1``);
* ``cannot_read``: a reviewer that answers but never read the snapshot (no token returned).

A reviewer step's answer gets ``snapshot_token`` (read from the snapshot's token file, as a
reviewer with real file access would) and ``inspection_complete: true`` unless it sets them.

Every invocation appends a line to ``<script>.log``: its pid, cwd, argv and the prompt,
and "start"/"end" markers, so tests can prove what ran, where, and that no two overlapped.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _step(script: Path) -> dict[str, object]:
    data: Any = json.loads(script.read_text(encoding="utf-8"))
    if isinstance(data, list):
        step: dict[str, object] = data[0] if data else {}
        if len(data) > 1:
            script.write_text(json.dumps(data[1:]), encoding="utf-8")
        return step
    whole: dict[str, object] = data
    return whole


def _log(script: Path, record: dict[str, object]) -> None:
    with open(script.with_suffix(".log"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


USAGE = {
    "input_tokens": 3,
    "cache_creation_input_tokens": 100,
    "cache_read_input_tokens": 50,
    "output_tokens": 20,
}


def _emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)  # flushed: a run killed later keeps what it printed


def _assistant(message_id: str, content: Mapping[str, object]) -> dict[str, object]:
    message = {"id": message_id, "role": "assistant", "content": [content], "usage": USAGE}
    return {"type": "assistant", "message": message}


def _option(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> int:
    cli, script, args = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    # Independent mocked inventories/auth: probes must not consume build/review steps.
    if cli == "codex":
        tail = list(args)
        features = {"plugins": "true", "remote_plugin": "true", "apps": "true"}
        while tail[:1] == ["--disable"]:
            features[tail[1]] = "false"
            tail = tail[2:]
        if tail[:2] == ["-c", 'windows.sandbox="unelevated"']:
            tail = tail[2:]
        if tail == ["features", "list"]:
            print("\n".join(f"{key} stable {value}" for key, value in features.items()))
            return 0
        if tail == ["--version"]:
            print("codex-cli 0.164.0")
            return 0
        if tail == ["exec", "--help"]:
            from forgeflow.providers import CodexProvider

            print(" ".join(CodexProvider.feature_flags))
            return 0
        probes = {
            ("mcp", "list", "--json"): "[]",
            ("plugin", "list", "--json"): '{"installed":[],"available":[]}',
            ("login", "status"): "Logged in using ChatGPT",
        }
        if tuple(tail) in probes:
            print(probes[tuple(tail)])
            return 0
    prompt = sys.stdin.read()
    step = _step(script)
    workdir = Path(_option(args, "-C", "--cd") or os.getcwd())
    record = {
        "pid": os.getpid(),
        "cwd": str(workdir),
        "argv": args,
        "prompt": prompt,
        "codex_home": os.environ.get("CODEX_HOME"),
        "bash_timeouts": [
            os.environ.get(v) for v in ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")
        ],
    }
    _log(script, {**record, "event": "start"})
    writes = dict(step.get("writes", {}))  # type: ignore[call-overload]
    if cli == "claude":
        _emit({"type": "system", "subtype": "init", "tools": ["Read", "Edit", "Write"]})
    for turn, (rel, content) in enumerate(writes.items(), start=1):
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        if cli == "claude":
            tool = {
                "type": "tool_use",
                "name": "Write",
                "input": {"file_path": rel, "content": content},
            }
            # Like the real CLI, one API message arrives as several events sharing id and usage.
            _emit(_assistant(f"msg_{turn}", {"type": "thinking", "thinking": ""}))
            _emit(_assistant(f"msg_{turn}", tool))
    time.sleep(float(str(step.get("sleep", 0))))  # after the writes: partial work stays behind
    if step.get("stderr"):
        sys.stderr.write(str(step["stderr"]) + "\n")
    review: Any = step.get("review")
    if isinstance(review, dict):
        # Like a real reviewer with file access, read the token ForgeFlow put in the snapshot;
        # "cannot_read" is the live failure: valid JSON, but the files were never readable.
        token_file = workdir / ".forgeflow-review-token"
        readable = token_file.exists() and not step.get("cannot_read")
        token = token_file.read_text(encoding="utf-8").strip() if readable else ""
        review = {"snapshot_token": token, "inspection_complete": True, **review}
    result = str(step.get("result", "done"))
    if cli == "claude":
        answer = json.dumps(review) if review is not None else result
        _emit(_assistant(f"msg_{len(writes) + 1}", {"type": "text", "text": answer}))
        output = {
            "type": "result",
            "subtype": "error_during_execution" if step.get("is_error") else "success",
            "is_error": bool(step.get("is_error")),
            "num_turns": len(writes) + 1,
            "result": answer,
            "total_cost_usd": 0.0123,
            "usage": USAGE,
            "permission_denials": step.get("denials", []),
        }
        if step.get("limit"):  # as Claude Code 2.1.316 reports either limit being reached
            output.update(
                subtype=f"error_{step['limit']}",
                is_error=True,
                terminal_reason="max_turns" if step["limit"] == "max_turns" else "budget_exhausted",
                errors=[f"Reached maximum {step['limit']}"],
                result=None,
            )
        if "--json-schema" in args:
            output["structured_output"] = review
        _emit(output)
    else:
        print(json.dumps({"type": "thread.started", "thread_id": "fake"}))
        if step.get("sandbox_refusal"):
            sys.stderr.write(
                "ERROR codex_core::tools::router: error=exec_command failed: CreateProcess "
                '{ message: "UnsupportedOperation(\\"windows unelevated restricted-token sandbox '
                "cannot enforce split writable root sets directly; refusing to run "
                'unsandboxed\\")" }\n'
            )
        print(
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 70, "output_tokens": 9}}
            )
        )
        last = _option(args, "-o", "--output-last-message")
        if last:
            message = json.dumps(review) if review is not None else result
            Path(last).write_text(message, encoding="utf-8")
    _log(script, {**record, "event": "end"})
    return int(str(step.get("exit", 0)))


if __name__ == "__main__":
    sys.exit(main())
