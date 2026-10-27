# Isolated Codex runtime

ForgeFlow-managed Codex processes use their own persistent profile:

| OS | `CODEX_HOME` |
| --- | --- |
| Windows | `%LOCALAPPDATA%\forgeflow\codex` (fallback: `%USERPROFILE%\AppData\Local\forgeflow\codex`) |
| Linux | `$XDG_DATA_HOME/forgeflow/codex` (fallback: `~/.local/share/forgeflow/codex`) |

The application-data path must be absolute, outside any Git repository and free of
symlinks/junctions. An inherited `CODEX_HOME` does not select the managed profile.
ForgeFlow changes child environments only; ordinary interactive `codex` and host skill
discovery continue using the user's existing configuration.

## First-time authentication

1. Install Codex CLI yourself. The runtime inventory commands were verified with 0.164.0.
2. Run `forgeflow doctor`. It may create the managed directory and a minimal
   `config.toml` containing only `cli_auth_credentials_store = "file"`. No personal
   configuration, plugins, credentials or `auth.json` are copied.
3. Follow the scoped `codex login` command printed by doctor **in your own terminal**.
   Windows instructions target PowerShell and start a separate PowerShell process;
   Linux instructions use a subshell and a command-scoped `CODEX_HOME`. Both log in
   from the managed profile directory, leaving the calling terminal's environment
   and working directory unchanged. Complete Codex's interactive authentication.
4. Re-run doctor, then run your approved job with Codex as builder and/or reviewer.

For example, with the default locations:

```powershell
# Paste into PowerShell after doctor initializes the profile:
powershell -NoProfile -Command '$env:CODEX_HOME = Join-Path $env:LOCALAPPDATA "forgeflow\codex"; Set-Location -LiteralPath $env:CODEX_HOME; codex login'
```

```sh
(cd "${XDG_DATA_HOME:-$HOME/.local/share}/forgeflow/codex" && CODEX_HOME="$PWD" codex login)
```

Use the exact command from doctor for fallback paths or custom application-data
locations. Do not copy your personal `auth.json`, add plugins, or customize this
profile. Credentials created by your independent login stay in its `auth.json`;
ForgeFlow never reads or prints their contents. It never attempts unattended login.

## Checks and guarantees

Before model execution, ForgeFlow checks the managed profile, known configuration
sources, `codex features list`, `codex mcp list --json`, `codex plugin list --json`,
and `codex login status`. All managed Codex commands use `--disable plugins
--disable remote_plugin --disable apps`. The feature probe must confirm all three
are disabled; unknown, missing, enabled, or duplicate values block execution.

This matters after authentication: Codex 0.164.0's plugin-list command can query the
account's remote catalog and time out even when the isolated profile has no plugins.
Disabling account-backed discovery avoids that lookup and prevents remote plugins
or account apps from being exposed to managed runs. The isolated profile remains
the primary configuration boundary; these switches supplement it rather than
disabling individual personal MCP servers.
Detection uses the managed profile as cwd; doctor and preflight inspect the selected
repository, and execution repeats the checks in the actual builder worktree or
reviewer snapshot. Missing CLI inventory support, failed/timed-out probes, unknown
JSON shapes, nonempty inventories, missing authentication, or inaccessible profile
paths block execution. There is no fallback to a personal profile.

The runner neutralizes inherited `CODEX_*` overrides and OpenAI API credentials in
the child environment. The managed file credential store avoids personal keyring
fallback. Raw inventory/auth diagnostics are not included in errors.

`CODEX_HOME` alone does not exclude project configuration. ForgeFlow therefore
refuses `.codex/config.toml`, `.codex/hooks.json`, `.codex/hooks`, `.codex/rules`,
`.agents/plugins/marketplace.json` and `.claude-plugin/marketplace.json` in the work
directory or its ancestors. The ordinary user `.codex` source is replaced by
`CODEX_HOME`. Detected system/legacy managed configuration is also refused rather
than bypassed: `/etc/codex` on Linux; `%ProgramData%\OpenAI\Codex` and personal
`~/.codex/managed_config.toml` on Windows. Unexpected managed-profile settings,
extensions and linked authentication files block execution without being rewritten.
Use a checkout without these configuration sources, or another provider. ForgeFlow
does not remove repository settings or change organization policy.

The existing workspace-write/read-only sandboxes, never-approve policy, timeouts,
process-tree cleanup, lease/PID tracking and review access verification still apply.
New execution results record the successful isolation check in provider notes;
historical evidence is not relabeled as isolated.

## Windows sandbox backend

On native Windows every managed Codex command (detection, verification probes and
execution) also receives `-c 'windows.sandbox="unelevated"'`. Without an explicit
backend Codex can start a read-only session even when ForgeFlow passes
`--sandbox workspace-write`: the builder then cannot inspect sources, run commands or
write, and changes nothing. Selecting the backend per process (not in the managed
`config.toml`) needs no profile migration, leaves authentication alone and never touches
the personal Codex configuration. Linux arguments are unchanged. Builders keep
`workspace-write`, reviewers `read-only`, approvals stay `never`; plugin/MCP isolation
is unchanged and `danger-full-access` is never used.

Limits: the unelevated backend uses a restricted token rather than a dedicated sandbox
user, so it is weaker than Codex's elevated backend. It limits writes to the workspace
but is not a security boundary against hostile code: reads are not fully confined, and it
does not hide the user's files or network. Worktrees under `%TEMP%` are still refused.
If a builder reports it cannot read, run or write, the result notes point here.

## Limits

This isolates **Codex configuration**, not the operating system. It does not hide
the user's filesystem, create a separate OS account, remove all inherited secrets,
or sandbox repository validation commands. Plain user/repository skills and
`AGENTS.md` instructions may still be discovered. Executable resolution still trusts
`PATH`. The profile directory is created with mode `0700` (Python 3.13+ applies a
restricted ACL on Windows); this does not create a separate security principal.

Simultaneous first-time profile initialization can fail closed if another process
has created but not finished writing the minimal config; retry doctor after it finishes.
Inventory checks are startup checks, not continuous monitoring or protection from
concurrent modification by another process running as the same user. Cloud/managed
policy and future Codex versions can change runtime behavior; only recognized empty
inventory responses are accepted. No organization policies are disabled. An
authenticated live builder/reviewer smoke test requires a human to log into this
independent profile; automated tests use mocked subprocesses instead. The doctor
path was also verified against a real, independently authenticated Codex 0.164.0
profile after the human completed login: authentication succeeded, all three
features were disabled, and MCP/plugin inventories were empty. No model was invoked.

Relevant upstream behavior: [configuration precedence](https://developers.openai.com/codex/config-basic),
[credential storage](https://developers.openai.com/codex/config-reference), and
[managed configuration](https://developers.openai.com/codex/enterprise/managed-configuration).
