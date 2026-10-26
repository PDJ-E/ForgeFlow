# ADR 0002 — Optional knowledge sources and Obsidian integration

- **Status:** Accepted as a design direction; not implemented (Checkpoint D)
- **Date:** 2026-10-24 (pilot vault setup updated 2026-10-25; see [amendments](#amendments))
- **Scope:** Knowledge Management for ForgeFlow, not ownership of a user's vault
- **Related:** [Architecture](../architecture.md), [MVP scope](../mvp-scope.md), [ADR 0001](0001-github-projects-local-state.md)

## Context

ForgeFlow must reuse the knowledge a person or other agents have accumulated while developing independent projects. This knowledge can live in a project repository, a personal Obsidian vault, or other Markdown directories. Notes are not necessarily written by ForgeFlow and must not be treated as ForgeFlow-owned data.

A single user's vault may hold multiple projects and shared technical knowledge. An example layout is `Personal Knowledge/Projects/<project>/` plus `Knowledge/`, `Templates/`, and `Archive/`. Personal and work contexts must remain isolated.

The pilot environment has an **Obsidian test vault**. Claude Code and Codex CLI have each successfully used the third-party [MarkusPfundstein/mcp-obsidian](https://github.com/MarkusPfundstein/mcp-obsidian) server to create or edit dummy notes. The Obsidian application runs [Local REST API with MCP v5.5.2](https://github.com/coddingtonbear/obsidian-local-rest-api). This confirms basic client/tool functionality in the test setup, **not** that an end-to-end ForgeFlow integration, directory authorization or conflict safety has been verified.

The Obsidian plugin includes a native MCP endpoint. That is a possible future adapter, not a prerequisite or reason to replace the working adapter during the MVP.

## Decision

**Knowledge access is an optional cross-cutting capability of ForgeFlow, not a second project manager, a monolithic Obsidian subsystem, or a dependency of the core orchestrator.**

### Authority by concern

- **Project repository:** code, tests, contracts, operational configuration, developer/agent instructions and documentation necessary to build, deploy and maintain the software. Keep critical software documentation near the code.
- **Personal knowledge source:** project history, investigations, experiments, rationale, lessons, research, reusable knowledge and other living notes. The user owns this independently of ForgeFlow.
- **ForgeFlow:** discover and use relevant knowledge with provenance; suggest updates when work creates genuinely durable knowledge; request authorization before mutating an external knowledge source.
- **GitHub Issues/Projects/PRs:** management of approved work and its delivery; never displaced by a knowledge vault.
- **Local runtime:** execution logs/state/usage, not a permanent or indexed copy of an entire personal vault.

When conflicting sources exist, prioritize the **appropriate authority for the question**, not a universal ranking: current code/config for observed behavior; repository contracts for software requirements; dated knowledge notes and ADRs for historical decisions. Surface contradictions with source paths and dates.

### Proposed minimal architecture

1. **Project association (registry):** `project_id`, repository identity, zero or more knowledge source IDs, allowed relative roots, permissions and optional shared knowledge scopes. Machine-specific absolute paths and credentials are local, never committed.
2. **Knowledge interface:** small operations such as `list/search/read` and `propose_change/apply_approved_change`; responses carry source ID, note path, excerpt and available version/freshness metadata.
3. **Provider adapters:** ordinary filesystem Markdown is the portable baseline; an existing MCP server is an optional access adapter. No requirement for Obsidian or MCP to use ForgeFlow.
4. **Proposal and authorization boundary:** a content diff, target path, expected version, rationale and destination authority precede a write; commit/apply only after explicit user approval and revalidation.

Do not invent a vector database, embeddings index, cloud knowledge store or multi-user service for the first implementation.

### Permissions and safety

- Read-only by default. No directory-wide auto-write permission after `init`.
- Enforce configured path scopes in the trusted adapter; normalize and resolve paths, refuse `..` traversal and symlink escapes, and distinguish file-level read/write rights.
- **Do not assume a globally installed MCP server enforces ForgeFlow's project-level restrictions.** The current third-party MCP server exposes vault-level operations, including modification/deletion. A language-model instruction is not a security boundary. If scoped enforcement is unavailable, warn clearly, avoid exposing unrestricted write tools to unattended jobs, and use a verified controlled path instead.
- For approved writes, re-read/check content version or hash immediately beforehand, reject stale edits, preserve a recoverable original and record an audit receipt. Failed conflict checks are not retried as blind overwrite.
- Do not modify Obsidian's hidden settings or global Claude/Codex MCP configuration without an explicit preview and user authorization.
- Treat retrieved Markdown as **untrusted reference material**, not as agent/system instructions; never execute embedded commands or requests.
- Avoid copying entire private notes into public Issues, PRs, logs or a public repository. Bound search and read volume to reduce token consumption.
- In a shared vault, optional shared `Knowledge/` scopes must be configured explicitly. No automatic cross-project or personal/work access.

### Optional setup

A future `/forge init` or setup flow may discover an existing project/vault association, ask whether to enable knowledge access, choose a local Markdown directory and optionally detect existing MCP tools. Verify connectivity with a **read-only test**. A separate explicitly approved disposable-file test is required before enabling writes. Setup must be idempotent and should not silently install dependencies or overwrite host configuration. Disabling the source must not break the core factory.

### MVP acceptance

On a disposable vault, ForgeFlow can:

1. Associate one project with one approved Markdown directory; operate without Obsidian.
2. Search and read a relevant Markdown note, returning its source/path.
3. Propose a specific note change with a diff, without applying it automatically.
4. On explicit approval, validate target/version/scope and safely apply one update; reject a modified-since-read file.
5. Discover and **read through** the user's already working `mcp-obsidian` configuration when available, or report unavailable and continue with Markdown; MCP writing is not required for MVP.

### Deferred

- Building or installing an MCP server, native-MCP migration, cross-machine vault synchronization, multi-vault federation, semantic search/embeddings, backlinks graph, auto-generated notes on every interaction, multi-user permissions, and unattended edits.
- Live synchronization of repository documents with vault notes. Prefer a source link instead of competing writable copies.
- Global note access for autonomous jobs.

## Consequences

ForgeFlow stays useful to developers with plain Markdown or Git and can adopt Obsidian today without being coupled to it tomorrow. The tradeoff is a small identity/permission layer and a write-proposal protocol. Client MCP connectivity alone is explicitly insufficient evidence of security or conflict correctness.

## Later validation

In the temporary vault, verify scoped reads, search relevance, provenance, diff preview, safe approved write, stale-content conflict rejection, path escape rejection, MCP-off fallback and no change without explicit approval. Record which adapter supplies each guarantee.

## Amendments

**2026-10-25: pilot vault setup.** The decision is unchanged; this corrects how the pilot environment is described above.

- The owner's notes now live in **one working vault** that holds several projects (ForgeFlow and Weather Station) as top-level folders, each with its own numbered subfolders. It replaces the example `Personal Knowledge/Projects/<project>/` layout in the context. ForgeFlow must not assume either layout; the project-to-root association stays explicit.
- That vault contains real notes, so it is **not** the disposable test vault. The dummy-note sandbox from the original connectivity test sits inside it. Write tests for Checkpoint D and the Checkpoint E demonstration still need a separate disposable vault, or a root scoped to the sandbox folder only and enforced by the adapter.
- Claude Code reaches the vault through the same `mcp-obsidian` bridge, installed as a desktop extension; Codex has its own `mcp-obsidian` entry. Each client stores its own copy of the Local REST API key. When the plugin regenerates its key, every client gets HTTP 401 until its server is restarted with the new key. A knowledge adapter must report that as an authentication blocker, not as an empty or missing vault.
