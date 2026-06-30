<%*
// Concept-group executive card. Hand-creation scaffold AND the kb-sync format contract.
// kb-sync emits this exact structure by writing the card directly to disk (frontmatter key order + headings).
// Mission Control classifiers (ADR-0002): folder slug -> witty display + PERIOD-FREE tag slug + emoji.
// Group slugs are an editable example taxonomy (see plugin README, Tier-2 note).
const GMAP = {"1.0-dev":["Launchpad","launchpad","🚀"],"1.1-dev-tools":["Tool Bay","toolbay","🛠️"],"2.0-career":["Trajectory","trajectory","📈"],"3.0-work":["Mission Ops","missionops","🛰️"],"5.0-home":["Ground Control","groundcontrol","🏡"]};
const group = await tp.system.suggester(Object.keys(GMAP), Object.keys(GMAP), false, "Concept group");
const cls = GMAP[group][0], slug = GMAP[group][1], emoji = GMAP[group][2];
const tierTag = await tp.system.suggester(["01-secops","02-platform","03-apps","none"], ["tier/01-secops","tier/02-platform","tier/03-apps",""], false, "Secondary tier tag (cross-cut)");
const rag = await tp.system.suggester(["green","yellow","red"], ["green","yellow","red"], false, "RAG flag");
const repo = await tp.system.prompt("Repo URL (https://github.com/org/repo)");
-%>
---
# --- generator-owned (kb-sync rewrites every sync; do not hand-edit) ---
type: project
title: "<% tp.file.title %>"
aliases: []
tags: [type/project, "group/<% slug %>"<% tierTag ? ', "' + tierTag + '"' : '' %>]
classifier: "<% cls %>"
group: "<% group %>"
source-file: ""
repo: "<% repo %>"
path: ''   # local repo path (<dev-root>/<group>/<name>); kb-sync fills from repo.path (SINGLE-QUOTED, YAML-safe); read by the board launcher and the Actions buttons
branch: main
last-documented-sha: ""
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
up: "[[01-groups/<% group %>]]"
related: []
docs: "docs/kb/"
# --- operator-owned (written only-if-blank; round-trips to repo frontmatter) ---
status: active
rag-flag: <% rag %>
blocker-severity: ""
blockers: []
next-action: ""
next-command: ""
notes: ""
---

# <% tp.file.title %>

> One-sentence purpose statement — what it is and why it matters.

> [!abstract] At a glance
> **<% emoji %> <% cls %>** · **RAG:** `INPUT[inlineSelect(option(green), option(yellow), option(red)):rag-flag]` · **Repo:** [`<% tp.file.title %>`](<% repo %>)

## 🚦 Operator next step

> [!todo] Do this next
> **`= this.next-action`**
>
> ```bash
> # = this.next-command
> ```
> _Why:_ <!-- one line; or set next-action to "needs-triage" if the repo states no next step -->

`BUTTON[mark-next-done]`

## ⛔ Blockers

> Severity drives the Home kanban. Empty = none.

| Blocker | Severity | Since | Unblock |
|---|---|---|---|
| <!-- generated from blockers[]; "None" if empty --> | | | |

## 🧭 Architecture (concise)

<!-- 2-4 sentences, agent-optimized. Link OUT to the repo's deep docs — never copy. -->

| Deep doc | Location |
|---|---|
| Overview | [overview.md](<% repo %>/blob/main/docs/kb/overview.md) |
| Architecture | [architecture.md](<% repo %>/blob/main/docs/kb/architecture.md) |
| CLI reference | [cli.md](<% repo %>/blob/main/docs/kb/cli.md) |
| Errors | [errors.md](<% repo %>/blob/main/docs/kb/errors.md) |
| Config | [config.md](<% repo %>/blob/main/docs/kb/config.md) |
| Dev loop | [dev-loop.md](<% repo %>/blob/main/docs/kb/dev-loop.md) |

## ⌨️ Key commands

```base
filters:
  and:
    - 'file.hasTag("type/cli")'
    - 'note.tool == "<% tp.file.title %>"'
views:
  - type: table
    name: Commands
    order:
      - file.name
      - note.command
```

## 🔗 Decisions · ADRs

```base
filters:
  and:
    - 'note.type == "adr"'
    - 'note.project == "<% tp.file.title %>"'
views:
  - type: table
    name: ADRs
    order:
      - file.name
      - note.status
      - note["date-decided"]
```

## 🧰 Relevant tools

> Claude Toolkit capabilities that share a `tool/`·`pattern/`·`capability/` tag with this project
> (ADR-0002 reuse projection). kb-sync rebuilds the `or:` list below from this card's reuse tags;
> an empty list is replaced with `_No shared-tag tools yet._`.

```base
filters:
  and:
    - 'file.inFolder("07-toolkit")'
    - or:
        - 'file.hasTag("pattern/static-site-generation")'
views:
  - type: table
    name: RelevantTools
    order:
      - file.name
      - note.classifier
```

## ⛔ Open blockers

```base
filters:
  and:
    - 'note.type == "blocker"'
    - 'note.project == "<% tp.file.title %>"'
    - 'note.stale != true'
views:
  - type: table
    name: Blockers
    order:
      - note["severity-rank"]
      - note.severity
      - note.since
      - note.text
      - note.unblock
```

## ⚡ Actions

> [!info] Click-gated only (spec §6 / PHANTOMPULSE [[obsidian-actionable-plugins]]) — nothing runs on open.
> Each button runs ONE visible Shell Command on click, reading this note's `local-path` (and `next-command`)
> frontmatter. Stateful runs prompt for confirmation. See [[actionable-dashboard-setup]] to define the commands.

```meta-bind-button
label: "▶ Run next step"
style: primary
action:
  type: command
  command: shell-commands:execute-kb-run-in-dir
```

```meta-bind-button
label: "📂 Open folder"
style: default
action:
  type: command
  command: shell-commands:execute-kb-open-folder
```

```meta-bind-button
label: "🖥 Terminal here"
style: default
action:
  type: command
  command: shell-commands:execute-kb-open-terminal
```

```meta-bind-button
label: "📝 Edit .env (OS editor)"
style: default
action:
  type: command
  command: shell-commands:execute-kb-edit-file
```
