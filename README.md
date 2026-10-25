# ForgeFlow

**A human-guided, multi-model AI software factory for Claude Code, Codex, and GitHub.**

> **Status: pre-alpha, unreleased.** The code currently provides Checkpoint A, a tested execution harness. It covers environment inspection, durable job state, worktree isolation, provider detection, GitHub issue linkage and read-only `status`. **No autonomous pipeline exists yet:** ForgeFlow does not run Claude or Codex on code, create issues or PRs, or merge. Nothing has been released. See [getting started](docs/getting-started.md).

## Why ForgeFlow?

AI coding assistants are useful on their own, but real software development begins before code and continues after it. ForgeFlow aims to connect product discovery, architecture, implementation, testing, independent review, and evidence-based learning into a workflow that stays under human control.

The objective is **reliable engineering with practical automation**, not maximum autonomous agent activity.

## Intended workflow

```text
Idea -> Discover -> Design -> HUMAN APPROVES PLAN
                                |
                                v
                       GitHub Issue / Project
                                |
                                v
                   Build (Claude or Codex)
                                |
                                v
                      Tests / QA / Review
                                |
                                v
                        Pull Request
                                |
                                v
                       HUMAN APPROVES MERGE
                                |
                                v
                         Learn / Improve
```

### Design principles

- **Human before automation:** discuss the problem and approve the architecture and scope before agents implement anything.
- **Provider-independent execution:** Claude Code and Codex CLI are intended to act as interchangeable builders or independent reviewers, subject to verified capabilities.
- **Reuse existing expertise:** integrate Anthropic's Engineering and Product Management plugins where available; avoid copying their skills or assuming they are installed in every host.
- **GitHub for project management:** GitHub Projects tracks approved work; Issues define implementation tasks; Pull Requests and CI provide reviewable delivery.
- **Local runtime evidence:** agent state, logs, model and token metadata, and execution artifacts remain local, with clear links to GitHub where useful.
- **One source of truth per concern:** no second Kanban board or duplicated authoritative task state.
- **Risk-proportional verification:** deterministic checks first, then relevant review; no unnecessary parallel-agent swarms.
- **Safe self-improvement:** collect evidence and propose tested process improvements; do not silently rewrite installed skills or security policies.
- **Optional Knowledge Management:** access project-scoped Markdown and optionally existing Obsidian MCP; keep user-owned vaults independent and require authorization for changes.

## Interfaces

The user-facing name is **ForgeFlow**. Its short entry points are `/forge` for Claude Code and `$forge` for Codex, provided as repository-scoped skills. Both were invoked successfully in the real clients during Checkpoint A. The underlying CLI is `forgeflow` (`python -m forgeflow`); it is not named `forge` because that name collides with other common tools.

| Operation | Status |
| --- | --- |
| `doctor`: environment, GitHub and provider inspection | implemented (read-only) |
| `status`: local job state | implemented (read-only) |
| `init`, `idea`, `design`, `approve` | planned, Checkpoint B |
| `run`, `review`, `rework`, `resume` with real providers | planned, Checkpoint C |
| knowledge sources (Markdown, optional Obsidian MCP) | planned, Checkpoint D |
| `learn` | after v0.1 |

Packaging the skills for use in other repositories has not been validated yet.

## Architecture and roadmap

- [Architecture and current design boundaries](docs/architecture.md)
- [ADR 0001 — GitHub Projects and local execution state](docs/decisions/0001-github-projects-local-state.md)
- [ADR 0002 — Optional knowledge sources and Obsidian](docs/decisions/0002-knowledge-sources-obsidian.md)
- [ADR 0003 — Runtime, local job state, locking and worktrees](docs/decisions/0003-runtime-state-and-worktrees.md)
- [Getting started (Checkpoint A, Windows)](docs/getting-started.md)
- [MVP v0.1 scope and acceptance criteria](docs/mvp-scope.md)
- [Development roadmap](docs/roadmap.md)
- [Changelog and release history](CHANGELOG.md)
- [Third-party references and attribution](ATTRIBUTION.md)

The first implementation target is [MVP v0.1](docs/mvp-scope.md): a bounded, auditable idea-to-human-reviewed-PR workflow, plus optional project-scoped Markdown knowledge access. It must be built and verified through small checkpoints in a **disposable test repository and vault**. The user's working Obsidian MCP setup is an existing external tool, not a bundled ForgeFlow integration. Only after testing should ForgeFlow be trialed on an actual project.

## Privacy and safety

ForgeFlow is intended to work with public or private repositories without making their contents public. The factory's own public repository must never include private project code, credentials, raw chats, API keys, or run logs containing sensitive data. High-risk operations, merging and production deployment require explicit authorization.

## Inspiration and acknowledgments

ForgeFlow is an **independent project** informed by [ZazenCodes' Software Factory V1/V2](https://github.com/zazencodes/zazencodes-season-3), [Eric Tech's Super Board](https://github.com/EricTechPro/super-board), and [Anthropic's Knowledge Work Plugins](https://github.com/anthropics/knowledge-work-plugins). We are grateful for the public work and documentation shared by their authors.

See [ATTRIBUTION.md](ATTRIBUTION.md) for source-level licensing distinctions and the policy for third-party code. Inspiration and links do **not** imply sponsorship, endorsement, or affiliation.

## Contributing

This project is in the design stage. Technical discussions, architecture critiques, and well-scoped suggestions are welcome through GitHub Issues. Please do not assume any advertised workflow is operational until an implementation and tests are published.

If contributing code, keep it original or clearly identify third-party sources and their license obligations. Never include secrets or proprietary material.

## License

ForgeFlow's **original content and code** is licensed under the [MIT License](LICENSE). This license does not override licenses on separately identified third-party materials. No ZazenCodes implementation code is intended to be incorporated into this initial documentation.
