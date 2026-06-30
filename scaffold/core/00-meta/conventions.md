---
type: reference
title: conventions
aliases: []
tags: [type/reference]
status: active
created: 2026-06-11
updated: 2026-06-16
related: ["[[tag-taxonomy]]"]
---

# conventions

Operator reference for note types, frontmatter schema, and naming rules. Distilled from `01-kb-architecture.md §5`.

---

## Naming rules

- **Kebab-case** filenames: `example-cli-adr-0001-subprocess.md`, `err-example-cli-2.md`.
- **filename = H1 = primary alias** — always in sync.
- **Prefix conventions:**

| Prefix | Usage | Example |
|---|---|---|
| `err-<tool>-<code>` | Atomic error/exit-code note | `err-example-cli-2.md` |
| `cmd-<tool>-<command>` | Atomic CLI command note | `cmd-example-cli-apply.md` |
| `<project>-adr-NNNN-<slug>` | ADR stub-card | `example-cli-adr-0001-subprocess-git-ssh.md` |

---

## Universal frontmatter (all note types)

```yaml
---
type: project|adr|cli|error|runbook|reference|moc|tool
title: "note-title"
aliases: []            # CLI names, error codes, abbreviations — drives [[wikilink]] resolution
tags: []               # controlled vocab — see 00-meta/tag-taxonomy.md
status: active|deprecated|draft|accepted|proposed|rejected|superseded
created: 2026-06-11    # UTC ISO-8601 date
updated: 2026-06-11
related: []            # ["[[note]]"] directional dependencies, run-with pairs
---
```

---

## Per-type frontmatter blocks

### type: project

```yaml
---
type: project
title: "project-name"
aliases: []
tags: [type/project, tier/01-secops]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
up: "[[01-secops]]"    # Breadcrumbs parent (tier MOC)
repo: "https://github.com/org/repo"   # GitHub remote (HTTPS); "" if unpublished
branch: main
live: false
rag-flag: green        # red | yellow | green
blockers: []           # [] or [{text, severity, since}]
next-action: ""        # operator prose: what to do next
next-command: ""       # copy-paste terminal command for the next step (may be blank)
last-documented-sha: ""
tier: "01-secops"      # 01-secops | 02-platform | 03-apps
docs: "docs/kb/"       # relative path to repo docs/kb/
kb_id: ""              # OPTIONAL visibility mirror of the repo's stable identity (uuid4). SOURCE OF TRUTH is the repo's root `.kb-id` file (see "Stable identity" below); the reconciler keys lifecycle ops on it, NOT on this card field.
# --- Mission Control swim-lane (operator-owned; written only-if-blank) ---
advances: ""           # end-goal lane (example enum): objective-a | objective-b | career | home | shared
phase: ""              # maturity column, left to right: seed | build | harden | ship
path: '<group>/<project>'      # dev-root-RELATIVE posix working dir (e.g. '1.1-dev-tools/my-tool'). Machine-neutral: each host resolves it against its dev root (see KB_DEV_ROOT below). NEVER store an absolute path here.
supports: []           # shared utilities only: which end-goal lanes this tool serves
# --- Data-plane operator fields (operator-owned; written only-if-blank) ---
nextsteps: []          # (string[]) what to do next, in order; replaces next-action (board reads nextsteps[0])
objective: ""          # (string) the final deliverable / outcome this project is working toward
file: ""               # (string) repo-relative path to the file to open at launch; joined with `path` for full path
problem: ""            # (string) statement of the problem this project solves
solution: ""           # (string) summary of the solution approach
adrs: []               # (references only) ADR note slugs/aliases; summaries are rendered from the ADR bodies, never copied here
# --- Remote working trees (governs CLAUDE.md §9 homelab-sync; default [] = none) ---
remote-trees: []       # [] => no homelab checkout => no SSH-sync step. Else: [{host, path, branch}]
---
```

### type: adr

```yaml
---
type: adr
title: "project-adr-NNNN-slug"
aliases: []
tags: [type/adr]
status: proposed       # proposed | accepted | rejected | superseded
created: 2026-06-11
updated: 2026-06-11
related: []
adr-id: "NNNN"
project: "project-name"
supersedes: ""
superseded-by: ""
deciders: []
date-decided: ""
source: "active/decisions/NNNN-slug.md"
---
```

### type: cli

```yaml
---
type: cli
title: "cmd-tool-command"
aliases: ["tool command", "tool command --flag"]
tags: [type/cli]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
up: "[[project-name]]" # Breadcrumbs parent (project card)
tool: "tool-name"
command: "command-name"
exit-code: 0
since: "v0.0.0"
---
```

### type: error

```yaml
---
type: error
title: "err-tool-code"
aliases: ["exit-N", "exact error message string"]
tags: [type/error]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
up: "[[project-name]]" # Breadcrumbs parent (project card)
tool: "tool-name"
code: "N"
exit-code: N
since: "v0.0.0"
---
```

### type: runbook

```yaml
---
type: runbook
title: "runbook-slug"
aliases: []
tags: [type/runbook]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
---
```

### type: reference

```yaml
---
type: reference
title: "reference-slug"
aliases: []
tags: [type/reference]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
url: "https://..."     # canonical upstream URL
---
```

### type: moc

```yaml
---
type: moc
title: "_INDEX"
aliases: []
tags: [type/moc]
status: active
created: 2026-06-11
updated: 2026-06-11
related: []
---
```

---

## Launcher contract

A project card's launcher (the ▶ Run next step button) composes the runnable command from frontmatter as:

```
cd "$KB_DEV_ROOT/<path>"; <next-command>
```

`<path>` is dev-root-relative, so the launcher prepends `$KB_DEV_ROOT` (set per machine — see below). The file launch target joins the resolved dir with the repo-relative `file` field:

```
$KB_DEV_ROOT + "/" + <path> + "/" + <file> = absolute path to file in editor
```

Both `path` and `next-command` are required for the launcher to function; `file` is optional (omit if no file needs opening).

## Dev-root resolution (`KB_DEV_ROOT`) — coexist across machines

Card `path:` is **dev-root-relative** so the same vault, on one shared git branch, works from multiple machines (e.g. a Windows workstation and a Linux host) without `path:` churn. Each machine resolves the absolute repo location as `<dev root> / <path>`, where the dev root is:

- `KB_DEV_ROOT` environment variable if set, else
- `~/repos` on all platforms (override with the `KB_DEV_ROOT` env var).

Consumers that resolve this: `kb-staleness.py` (+ `/kb-status`, the freshness engine via `kb_paths.resolve_repo`), the `kb-session-status.js` / `kb-commit-nudge.js` hooks (the nudge matches cards by `repo:` URL, not path), the `workflow.js` synth (writes `path_rel` + the `$KB_DEV_ROOT`-prefixed cd block), and the board (displays the relative path as-is). The single source of truth for the resolution is the kb-sync skill's `kb_paths.py` (Python) mirrored by the inline `KB_DEV_ROOT` logic in the JS hooks.

---

## Stable identity (`.kb-id`) — survives rename / relocate / repo-rename

Each project repo carries a **root `.kb-id` file** (`kb_id: <uuid4>`) — the project's
**invariant identity** and the single source of truth. `name` (the directory basename,
which drives card stem, scout-cache basename, atomic `<owner>`, edges key, wikilinks) is a
**mutable slug**; `kb_id` is what stays constant when a dir is renamed, moved between groups,
or its GitHub repo is renamed. The reconciler keys lifecycle ops on `kb_id` (falling back to
`name`), so a name change is recognised as a *rename*, not a death + rebirth.

- **Source of truth:** the repo's `.kb-id` file. The card's `kb_id:` frontmatter (if present)
  is an optional visibility mirror only — never load-bearing.
- **Baseline:** the reconciler keeps `reconcile/identity.yaml` (`kb_id → {name, group}`), a
  deterministic filesystem snapshot of live `.kb-id` files written by `reconciler stamp` /
  `apply`. `reconciler detect` diffs the live scan against it to propose lifecycle ops.
- **Lifecycle:** mint + backfill with `reconciler stamp --commit`; see `tools/reconciler/README.md`
  and ADR-0008.

---

## Properties: YAML only

Always use YAML frontmatter delimited by `---`. Never use Dataview inline syntax (`key:: value`) — Bases requires YAML.

---

## Classifier → Template → Folder (auto-templating)

When you create a new note in Obsidian, Templater automatically applies the canonical template
for that folder. The same templates are the format contract for the kb-sync generation pipeline —
so hand-created notes and pipeline-generated notes share an identical structure.

| Note type / level | Template file | Folder rule (Templater auto-applies on new file) |
|---|---|---|
| Project hub card | `_templates/project.md` | `02-projects/` |
| ADR stub-card | `_templates/adr.md` | `03-adr/` |
| Atomic CLI command | `_templates/cli.md` | `04-cli-errors/` (via atomic.md dispatcher, `cmd-` prefix) |
| Atomic error note | `_templates/error.md` | `04-cli-errors/` (via atomic.md dispatcher, `err-` prefix) |
| Operational runbook | `_templates/runbook.md` | `05-runbooks/` |
| External reference | `_templates/reference.md` | `06-reference/` |
| Tier MOC | `_templates/tier-moc.md` | `01-tiers/` |
| System map (_INDEX) | `_templates/system-map.md` | One-time scaffold; regenerated by kb-sync |
| Generic MOC | `_templates/moc.md` | Hand-applied via Templater command palette |
| Dispatcher (04 folder) | `_templates/atomic.md` | `04-cli-errors/` (routes to cli or error by filename prefix) |

### kb-sync is the other consumer

The kb-sync pipeline conforms all generated notes (project cards, ADR stubs, CLI/error atomics)
to the same templates above. This establishes a **single format contract** for the vault:
whether a note is hand-created via Obsidian Templater or machine-generated by kb-sync, it
has identical frontmatter keys, key order, and section headings. Drift between the two
consumers is a defect — any change to a template applies equally to both paths.

---

## project-edges.yaml lineage fields

`00-meta/project-edges.yaml` is the operator-canonical source for three lineage fields that
kb-sync's synth generator projects onto generated project cards. These are **not** graph
edges — they are metadata surfaced downstream by the harvest/synth layer.

| Field | Type | Description |
|---|---|---|
| `advances` | `str\|None` | Swim-lane this project advances. Example enum: `objective-a \| objective-b \| career \| home \| shared` (define your own objective lanes) |
| `phase` | `str\|None` | Current maturity column (left→right). Enum: `seed \| build \| harden \| ship` |
| `milestones` | `list[dict]` | Ordered milestones as an inline pipe-delimited list (see below) |

`milestones` format — each entry is `"title|phase|status"`:

```yaml
my-project:
  advances: objective-a
  phase: build
  milestones: ["MVP|build|done", "Beta|harden|todo", "GA|ship|todo"]
```

Each entry parses to `{"title": str, "phase": str|None, "status": str}`. Missing `phase`
→ `None`; missing `status` → `"todo"`; blank title entries are skipped.

**Relationship to project-card frontmatter:** the `advances` and `phase` keys also appear
as project-card frontmatter fields (see `type: project` schema above, written only-if-blank).
`project-edges.yaml` is the **canonical source**; the synth generator writes these values
onto cards. Do not set them directly in the card if the sidecar governs the project — the
next sync run will overwrite them.

The parser is **liberal**: enum values are not validated at read time. Validation lives in
the writer (`kb-edge-draft`). Unknown keys in project-edges.yaml are silently ignored.
