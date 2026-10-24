# Inspiration, credits, and third-party licensing

ForgeFlow is an independent open-source project. The following work influenced its goals and proposed architecture. Attribution does not imply that any author sponsors, endorses, or is affiliated with ForgeFlow.

## Primary references

### Eric Tech — Super Board

- Source: https://github.com/EricTechPro/super-board
- License: MIT (verified in the repository's `LICENSE` file on 2026-10-24).
- Relevant ideas: GitHub Projects workflow, distinct Build / QA / Review lanes, worktree isolation, protective hooks, verified merge gates, provider/host selection, problem collection, and bounded post-run calibration.
- Reuse policy: original implementations preferred. If any Super Board code is copied or substantially adapted later, retain the required copyright and license notice for those files or substantial portions.

### ZazenCodes — Software Factory V1

- Source: https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code
- License status: **no applicable LICENSE file identified in V1 or at repository root during the 2026-10-24 review**.
- Relevant ideas: minimal local job state, short factory entry point, phase-oriented agent orchestration, lightweight local status dashboard.
- Reuse policy: architectural inspiration only. **Do not copy, vendor, or adapt V1 implementation text or code without establishing permission.** Public source availability is not a grant of a reuse license.

### ZazenCodes — Software Factory V2

- Source: https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2
- License: GNU GPL v3, supplied inside its own directory.
- Relevant ideas: issue-centered execution, risk-aware model routing, structured review artifacts, durable run state, verification gates and interruption recovery.
- Reuse policy: derive concepts and design an original implementation. Do not integrate GPLv3 implementation files into ForgeFlow's MIT-licensed original codebase without first conducting an explicit compatibility and distribution analysis and agreeing to the applicable obligations.

### Anthropic — Knowledge Work Plugins

- Source: https://github.com/anthropics/knowledge-work-plugins
- License: Apache License 2.0 (repository and Product Management plugin; verified 2026-10-24).
- Relevant modules: Engineering, Product Management, optionally Data and Productivity.
- Reuse policy: **integrate the user's installed plugins as external capabilities**, rather than copying their bundled prompts, scripts, or skills into ForgeFlow. Verify each host's actual access to them and respect the licenses/notices of any files subsequently incorporated.

## Additional ecosystem references

Super Board credits other open-source projects, including:

- https://github.com/obra/superpowers (MIT at time of review)
- https://github.com/mattpocock/skills (MIT at time of review)

These projects are not currently ForgeFlow dependencies. If we add them, we will review the exact version and corresponding notices.

## Rules for future contributions

1. Prefer original implementation from independently written design specifications.
2. Track the source URL, source commit or version, license, and modified files whenever third-party code is incorporated.
3. Retain required copyright, license texts, NOTICE files, patent/trademark notices, and modifications notices as applicable.
4. Do not assume that all files in a repository share the root license; check subdirectories, dependencies, images, documentation, and generated code.
5. Use clear compatibility boundaries for externally installed skills, MCP servers, and providers.
6. No trademark affiliation is claimed by mentioning Claude, Codex, GitHub, Context7, or other third-party products.
7. If provenance or license compatibility is uncertain, do not vendor the material; open an issue for review first.

## Disclaimer

This file is a practical source and license inventory, not legal advice or a complete dependency audit. Each future third-party inclusion requires a fresh file/version-specific license check. ForgeFlow's [MIT license](LICENSE) applies to ForgeFlow's original authored materials, not to independently licensed upstream materials.
