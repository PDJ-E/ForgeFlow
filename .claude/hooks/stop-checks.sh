#!/usr/bin/env bash
# Stop hook: before Claude ends a turn, changed Python files must pass ruff and the tree must
# pass mypy. Exit 2 sends the output back to Claude and keeps it working (see ADR 0006).
# A second consecutive failure (stop_hook_active) ends the turn with a visible warning instead
# of blocking again, so a failure the model cannot fix does not loop.
# Unit tests are not run here (the full suite takes minutes); AGENTS.md requires them per task.
set -u
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
input=""
[ -t 0 ] || input=$(cat)

if command -v py >/dev/null 2>&1 && py -3.13 -c "" >/dev/null 2>&1; then
  PY=(py -3.13)
elif command -v python3.13 >/dev/null 2>&1; then
  PY=(python3.13)
else
  echo "stop-checks: Python 3.13 not found; checks skipped" >&2
  exit 0
fi

# Changed or new Python files relative to HEAD (tracked and untracked, not ignored).
mapfile -t FILES < <( { git -c core.quotePath=false diff --name-only --diff-filter=ACMR HEAD -- '*.py';
                        git -c core.quotePath=false ls-files --others --exclude-standard -- '*.py'; } | sort -u )
[ "${#FILES[@]}" -eq 0 ] && exit 0

if ! "${PY[@]}" -m ruff --version >/dev/null 2>&1 || ! "${PY[@]}" -m mypy --version >/dev/null 2>&1; then
  echo "stop-checks: ruff/mypy not installed (py -3.13 -m pip install --user --group lint); skipped" >&2
  exit 0
fi

out=""
fail=0
r=$("${PY[@]}" -m ruff check "${FILES[@]}" 2>&1) || { fail=1; out+=$'\n[ruff check]\n'"$r"; }
r=$("${PY[@]}" -m ruff format --check "${FILES[@]}" 2>&1) || { fail=1; out+=$'\n[ruff format --check] run: py -3.13 -m ruff format <files>\n'"$r"; }
r=$("${PY[@]}" -m mypy 2>&1) || { fail=1; out+=$'\n[mypy]\n'"$(printf '%s' "$r" | tail -n 40)"; }

[ "$fail" -eq 0 ] && exit 0
if printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  printf 'stop-checks: THE TREE IS RED. Checks still fail after one retry; the turn ends anyway:%s\n' "$out" |
    "${PY[@]}" -c 'import json, sys; print(json.dumps({"systemMessage": sys.stdin.buffer.read().decode("utf-8", "replace")}))'
  exit 0
fi
printf 'Checks failed on changed files; fix them before finishing:%s\n' "$out" >&2
exit 2
