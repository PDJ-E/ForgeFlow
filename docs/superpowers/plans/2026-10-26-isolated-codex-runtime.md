# Isolated Codex runtime implementation plan

**Goal:** Run every ForgeFlow Codex subprocess with a persistent, minimal per-user
application-data profile, independent authentication, and fail-closed verification.

**Architecture:** A small `codex_runtime` helper owns profile preparation, child-only
environment overrides, configuration-source guards, and CLI inventory/auth probes.
Providers reuse the existing runners, task validation, leases, sandbox arguments,
timeouts, and review verification. Interactive host skill discovery remains unchanged.

**Constraints:** Standard library only; Windows and Linux; no copied credentials,
personal configuration changes, unattended login, merge, or next checkpoint.

- [x] Add focused failing tests for profile paths, environment propagation, independent
  authentication, repository inheritance, inventory failures, and both execution roles.
- [x] Implement profile preparation and verification and connect detection, doctor,
  preflight and execution using existing subprocess abstractions.
- [x] Preserve existing fake CLI/process regression tests with isolated test profiles.
- [x] Document interactive setup, strict configuration guards and the limits of
  configuration isolation; update the previous MCP limitation.
- [x] Run full unittest suite, Ruff and mypy, request independent code review, and
  resolve blocking findings (none found). Final local suite: 348 tests, 10 skipped.

Delivery: create the dedicated feature PR and wait for the complete Windows/Linux
CI matrix. Leave the PR unmerged; do not start another checkpoint.

Design decisions: reject repository/ancestor Codex configuration and plugin marketplaces
instead of rewriting them or relying on per-server overrides. Force file-based auth in
the managed profile to avoid shared credential-store fallback. Inspect JSON MCP/plugin
inventories in the actual subprocess cwd, and block unknown/failed responses.
