# ForgeFlow

**A human-guided, multi-model AI software factory for Claude Code, Codex, and GitHub.**

> **Status: design / pre-alpha.** ForgeFlow is a proposed open-source project. No working `/forge` command, CLI, installer, autonomous pipeline, or supported integration has been released yet. The documents in this repository record design decisions and a proposed roadmap, not shipped features.

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

## Intended interfaces (not implemented)

The proposed user-facing name is **ForgeFlow** with a short entry point, `/forge` for Claude Code and, where supported, `$forge` for Codex. Exact syntax, distribution packaging, and command collision behavior must be validated before release.

Potential operations include exploring ideas, designing solutions, approving plans, running approved jobs, reviewing results, inspecting status, and proposing lessons.

## Architecture and roadmap

- [Architecture and current design boundaries](docs/architecture.md)
- [ADR 0001 — GitHub Projects and local execution state](docs/decisions/0001-github-projects-local-state.md)
- [ADR 0002 — Optional knowledge sources and Obsidian](docs/decisions/0002-knowledge-sources-obsidian.md)
- [MVP v0.1 scope and acceptance criteria](docs/mvp-scope.md)
- [Development roadmap](docs/roadmap.md)
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
