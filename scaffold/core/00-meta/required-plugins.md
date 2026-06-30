---
type: reference
title: required-plugins
aliases: ["required plugins", "plugin manifest", "kb-sync --check"]
tags: [type/reference]
status: active
created: 2026-06-13
updated: 2026-06-13
related: ["[[0001-kb-plugin-anti-drift-architecture]]", "[[conventions]]"]
---

# Required plugins — the `kb-sync --check` contract

> The cure for the "declared-but-not-installed → raw-code render" bug (ADR-0001 D-7). `kb-sync --check`
> verifies every LOAD-BEARING plugin is installed+enabled before generating; a missing one fails fast.
> A fresh clone reproduces the experience by installing these (run `C:\tmp\get-plugins.ps1`-style fetch).

## Load-bearing (KB does not render correctly without these)
| Plugin | id | Role |
|---|---|---|
| Bases (CORE) | — | All dashboard tables/boards (Home, group MOCs, card Bases blocks). Core, cannot go missing. |
| Dataview | `dataview` | Inline-task rollups; `= this.x` (additive — not load-bearing for cards). |
| Tasks | `obsidian-tasks-plugin` | Operator task board rollup on Home. |
| Templater | `templater-obsidian` | New-note scaffolds from `_templates/`. |
| Meta Bind | `obsidian-meta-bind-plugin` | Interactive `rag-flag` dropdown in the card "At a glance" callout. |
| Local REST API + MCP Tools | `obsidian-local-rest-api`, `mcp-tools` | Agent read/validate access to THIS vault (must be the OPEN vault). |
| Style Settings | `obsidian-style-settings` | Tunable accents for `.obsidian/snippets/kb.css`. |
| Folder Notes | `folder-notes` | Folder→`_INDEX.md` association (config: index name = fixed `_INDEX`). |

## Load-bearing for ACTIONS (click-gated only — [[actionable-dashboard-setup]], spec 04 §6)
The per-card **⚡ Actions** buttons need BOTH of these. **Security floor is non-negotiable**: click-gated only,
NO Shell Commands event triggers (startup/timer/open), confirmation ON for stateful runs, secrets never imported
into the vault (edit configs via the OS editor). PHANTOMPULSE (Elastic 2026) weaponized auto-exec — see the runbook.
| Plugin | id | Role (ACTIONS) |
|---|---|---|
| Shell Commands | `obsidian-shellcommands` | Runs the 4 generic `kb-*` commands (run-in-dir / open-folder / open-terminal / edit-file). Desktop-only. |
| Meta Bind | `obsidian-meta-bind-plugin` | Renders the click-gated `meta-bind-button` blocks that invoke the `kb-*` commands. |

## Anti-drift contract (obsidian-linter) — ADR-0001 D-5
The generator is authoritative; Linter must be **zero-diff on generated cards**. Keep these Linter rules
**DISABLED** (or add `02-projects`, `03-adr`, `04-cli-errors`, `07-toolkit` to `foldersToIgnore`):
- **Sort YAML Key / format-yaml-array** — would reorder the owner-split frontmatter + reformat tag arrays.
- **YAML Timestamp (modified-on-save)** — the churn trap; must stay off.
Current operator config already has these disabled — verified. If Linter ever diffs a generated card, **fix the
emitter (`kb-sync` synth prompt), not Linter** (D-5).

## Note
The operator runs a much larger plugin suite (kanban, smart-connections, full-calendar, waypoint, beautitab,
git, commander, iconic, …) — those are optional/personal and NOT required for KB correctness. Only the
load-bearing set above gates `kb-sync --check`.
