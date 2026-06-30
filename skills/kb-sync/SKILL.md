---
name: kb-sync
description: Keep the Obsidian/agent knowledge base current. Re-documents changed repos using zero-context Haiku scouts + Sonnet synthesis, writing executive cards directly to disk. Invoke when the user says "sync the KB", "update the knowledge base", "kb-sync", or after shipping changes across projects. Change-targeted and low-cost by construction.
---

# kb-sync — concept-group knowledge-base generator

Maintains the unified KB defined in `<vault>/active/plan/kb-unification/00-spec.md`:
one frontmatter-rich corpus organized by **concept group** (`1.0-dev`, `1.1-dev-tools`,
`2.0-career`, `3.0-work`, `5.0-home`); tier is a secondary tag. **Generator-first: content is
never hand-edited** — the generator is authoritative (ADR-0001). Standalone forks are catalog-only.

## Preconditions (the `--check` gate)
- **Vault writes are direct DISK I/O** (the Obsidian vault indexes disk writes fine — the
  earlier "ReFS file-watch / REST-API-only" theory was wrong). Synth agents
  Write cards to disk; `kb-atomize.py` writes atomics to disk. Obsidian renders them on its normal refresh.
- Required plugins installed+enabled (bases, dataview, tasks, templater, local-rest-api, linter,
  meta-bind, folder-notes, …) per `00-meta/required-plugins.md`. A missing load-bearing plugin =
  raw-code render (the original bug); `--check` fails fast if any is absent.

## When to run
- After shipping work across one or more first-party repos. `--all` = full rebuild; `--repo <name>` = one.

## Flow
### Phase 1 — change detection (`kb-change-detect.py`, ~0 tokens, no LLM)
Run `kb-change-detect.py` to build the filtered work-list in one shot:
```bash
python3 skills/kb-sync/kb-change-detect.py --vault "$KB_ROOT" [--all] [--repo <name>]
```
Reads `00-meta/kb-manifest.json` for `last_documented_sha` per project, runs `git rev-parse HEAD`
in each repo under `$KB_DEV_ROOT/<group>/`, and outputs only repos where the sha changed as JSON
`[{name, group, path, path_rel, head_sha, last_documented_sha, changed_files_count}]`.
Pass `--all` for a full rebuild (bypasses sha comparison). Pass `--repo <name>` for a targeted run.
`path_rel` (dev-root-relative posix) and `last_documented_sha` are pre-populated and ready to
pass directly to workflow.js repos[].

### Phase 2A — deterministic structured harvest (`kb-harvest.py`, NO LLM)
Run `kb-harvest.py` once per changed repo. Writes structured keys — `cli`, `errors`, `adrs`, `identity`,
`docs_present`, `harvest_counts` — directly to `00-meta/scout-cache/<name>.json`. No network, no LLM;
idempotent and unit-tested. Each parser is isolated: a parser failure on one framework logs + leaves that
facet empty (and sets `harvest_cli_empty:true` in the args passed to Phase 2B if cli is empty), without
aborting the run. Per-facet LLM fallback in Phase 2B fills the exotic tail.

`kb-harvest.py` argument per repo: `{path, name, group, head_sha, changed_files}`
Output written: `<vault>/00-meta/scout-cache/<name>.json` (structured keys only at this point).

### Phase 2B — slim LLM prose pass (`workflow.js` scout, Haiku)
Zero-context **Haiku** scouts run one per repo, augmenting the cache with **prose/judgment only**
(`schemas/scout-output.json`): `summary`, `nextsteps`, `next_command`, `blockers`, `architecture.summary`,
`reuse_tags`, `problem`, `solution`, `objective`, `file`. The structured inventory is already in the cache from Phase 2A — scouts do NOT re-extract
cli/errors/adrs/docs_present and MUST NOT emit them.

`harvest_cli_empty:true` in the repo arg triggers the optional `fallback_cli` signal: the scout MAY emit a
low-confidence CLI array when deterministic harvest found no commands (default: omit).

The scout JSON is validated by `validateScout` (prose-key presence + blocker slug sanity), with one retry on
failure; a repo that fails both passes is dropped (no partial card).

### Phase 3 — Sonnet synth (card write, `workflow.js`)
The merged scout-cache (Phase 2A structured + Phase 2B prose) is passed inline to **Sonnet** synth, which
writes the **executive card** to `02-projects/<group>/<name>.md` to disk, conforming to
`_templates/project.md` (owner-split frontmatter, /explanatory-output body, Bases blocks, Meta Bind controls).

Synth also: emits the **deep-doc table CONDITIONALLY** — one row per file in `docs_present` from the cache
(empty ⇒ table omitted, `_No published docs/kb yet._`); and writes the verbatim merged cache JSON to
`00-meta/scout-cache/<name>.json` (the committed reproducibility anchor).

**Merge wiring (done — ADR-0005):** synth Reads the Phase-2A structured cache, merges the prose keys
(`{...harvestData, ...proseData}`), renders the card from the MERGED whole (`identity`/`cli`/`errors`/`adrs`/
`docs_present` come from Phase 2A), and writes the MERGED object back to `scout-cache` — never prose-only, which
would clobber the structured keys and break the atomic projection.

#### Workflow args contract (per-machine)
The orchestrator passes these args to the Workflow tool:
```json
{
  "vault": "<absolute path to knowledge-base vault>",
  "scoutPrompt": "<absolute path to kb-sync/scout-prompt.md>",
  "template": "<absolute path to knowledge-base/_templates/project.md>",
  "spec": "<absolute path to kb-unification/00-spec.md>",
  "buildAggregates": true,
  "now": "<ISO-8601 UTC timestamp>",
  "repos": [
    {
      "path": "<absolute repo working path — for scout reads>",
      "path_rel": "<dev-root-relative posix — written into card path: and cd block>",
      "name": "repo-name",
      "group": "1.1-dev-tools",
      "head_sha": "abc123",
      "changed_files": ["src/foo.py"],
      "last_documented_sha": "prev123"
    }
  ]
}
```
`vault` is **required** — pass it explicitly (or set `KB_VAULT`); the workflow throws if neither is set. `scoutPrompt` defaults to the skill-dir-relative `scout-prompt.md` when `CLAUDE_PLUGIN_ROOT` or `__dirname` is available; `template` defaults to `CLAUDE_PLUGIN_ROOT`-relative when set; `spec` is optional (omit or pass explicitly). **`KB_DEV_ROOT` must be set on the target machine** for the generated `cd "$KB_DEV_ROOT/${path_rel}"` blocks in cards to resolve at runtime.

**Linux example** (Linux, `KB_DEV_ROOT=~/repos`, vault at `~/repos/knowledge-base`):
```json
{
  "vault": "/home/you/repos/knowledge-base",
  "scoutPrompt": "/home/you/repos/knowledge-base/../.claude/skills/kb-sync/scout-prompt.md",
  "template": "/home/you/repos/knowledge-base/_templates/project.md",
  "repos": [
    {
      "path": "/home/you/repos/1.1-dev-tools/my-tool",
      "path_rel": "1.1-dev-tools/my-tool",
      "name": "my-tool",
      "group": "1.1-dev-tools",
      "head_sha": "abc123",
      "changed_files": []
    }
  ]
}
```
The generated card's `path: '1.1-dev-tools/my-tool'` and its cd block `cd "$KB_DEV_ROOT/1.1-dev-tools/my-tool"` both resolve correctly when `KB_DEV_ROOT=~/repos`.

### Phase 4 — atomic layer (deterministic post-pass: `kb-atomize.py`)
After the Workflow, run the deterministic projector — NO LLM, idempotent, unit-tested:
`py -3 "${CLAUDE_PLUGIN_ROOT}/skills/kb-sync/kb-atomize.py" --cache "<vault>/00-meta/scout-cache" --vault "<vault>" --date <YYYY-MM-DD>`
It projects `cmd-`/`err-` notes (`04-cli-errors/`) + ADR stubs (`03-adr/`) from the scout-cache and regenerates
both `_INDEX.md` routing tables between `<!-- KB-SYNC:ROWS:START/END -->` markers. **Merge semantics (ADR-0005,
relaxes ADR-0004):** CLI/errors/ADRs are projected by default with deterministic slugs and **accumulate-only**
absence — add new, update existing in place (by stable slug), **leave an absent slug UNTOUCHED (never stale-flag)**;
only **blockers** stale-flag on absence (absence == resolved). DELETE only owners listed in
`00-meta/retired-projects.txt`. `--frozen-errors-adrs` is the opt-OUT (freeze err/ADR for a run). Run `--only <name>`
to scope a single repo during validation. (Group MOCs / `Home.md` / `kb.css` / `kb-manifest.json` are hand-authored-stable or separately generated.)

### Phase 4B — retrieval index + agent query (`kb-index.py` / `kb-query.py`, NO LLM)
After the atomic layer is projected, rebuild the generated SQLite index (ADR-0005):
`py -3 "${CLAUDE_PLUGIN_ROOT}/skills/kb-sync/kb-index.py" --vault "<vault>" --out "<vault>/00-meta/kb.sqlite"`
Scans the markdown corpus into a `notes` table + FTS5 `notes_fts(title, body)`; atomic temp+`os.replace`, idempotent,
malformed notes skipped not fatal. `kb.sqlite` is **gitignored** (rebuildable derivative — rebuild after a pull).
Agents retrieve via the read-only, token-minimal `kb-query.py --db 00-meta/kb.sqlite [--type/--project/--tool/--severity]
[TEXT]` (structured filters intersect; positional `TEXT` is an FTS MATCH; `--json` for structured, `--with-body` for
bodies). `llms.txt` routes cold-start agents here instead of whole-file reads.

### Phase 5 — commit (orchestrator; NEVER a subagent — global §10)
Vault repo (`<vault>`) → commit + push (cards, scout-cache, atomic notes, `_INDEX`). The kb-sync
SKILL code lives in the `~/.claude` dotfiles repo (or `${CLAUDE_PLUGIN_ROOT}`) — versioned separately by the operator. obsidian-git
auto-commit/push stays OFF.

## Validation gate (before any `--all`)
Prove the pipeline on ONE repo, then 1–2 more, BEFORE a full `--all` (never blind): run the Workflow + `kb-atomize.py --only <name>`,
confirm the card's `note.tool`/`note.project` Bases blocks resolve and a re-run of `kb-atomize.py` yields ZERO git diff (idempotent).

## Invariants
- **Provenance split (spec §2 D2):** structured facts (cli/errors/adrs/identity/docs_present) come from
  `kb-harvest.py` (deterministic, unit-tested, no LLM). The LLM prose pass produces only judgment keys.
  Neither phase writes structured keys on behalf of the other.
- Prose scouts zero-context (Haiku, read-only, one repo, PROMPT INTEGRITY + SCOPE, "absence is data, never invent").
- **next_command is harvested or null — never fabricated** (`needs-triage` when the repo states no next step).
- Owner-split frontmatter: generator-owned fields always rewritten; operator-owned (rag-flag, next-action
  status, notes) written only-if-blank and round-tripped back to the repo (ADR-0001 D-2).
- Idempotent: unchanged repo ⇒ identical card ⇒ no diff.

## Files
- `workflow.js` — prose-scout→synth engine: Phase-1 sha-skip (orchestrator-fed `last_documented_sha`),
  `validateScout` gate (prose-key presence + blocker slug sanity, retry-once-then-drop), synth writes card
  to DISK + merged scout-cache dump.
- `scout-prompt.md` — Phase B prose-only contract (summary/nextsteps/next_command/blockers/architecture/
  reuse_tags/problem/solution/objective/file; MUST NOT emit structured inventory keys already in cache from kb-harvest.py).
- `schemas/scout-output.json` — prose-only schema (mirrored by inline `SCOUT_SCHEMA` in workflow.js);
  structured keys are NOT in this schema — they live in the Phase 2A harvest cache.
- `kb-harvest.py` — Phase 2A deterministic structured harvester (NO LLM): identity/docs + CLI (argparse/click/typer
  AST, make, npm) + errors (exception classes, `sys.exit`) + ADR metadata. Walks first-party source only —
  `IGNORED_DIR_NAMES` + pruned `os.walk` skip `.venv`/`site-packages`/`node_modules`/caches/`*.egg-info` (a vendored
  walk would inflate counts — see ADR-0005 validation). Writes structured keys, preserving prose keys. `tests/` covers it.
- `kb-atomize.py` — deterministic atomic-note projector + `_INDEX` regen (per-layer merge: CLI/err/ADR accumulate-only,
  blockers stale-on-absence; ADR-0005 relaxes ADR-0004). `--frozen-errors-adrs` opt-out. `tests/` has the pytest suite.
- `kb-index.py` — builds `00-meta/kb.sqlite` (`notes` + FTS5) from the markdown corpus (atomic, idempotent; gitignored output).
- `kb-query.py` — read-only, token-minimal agent retrieval over `kb.sqlite` (structured filters + FTS; TSV/`--json`).
- `<vault>/00-meta/scout-cache/` — committed per-repo scout JSON (reproducibility anchor).
- `<vault>/00-meta/retired-projects.txt` — explicit prune allowlist (empty = nothing pruned).

## Self-test
1. `--repo <a-project>` on unchanged HEAD ⇒ Phase 1 SKIP, zero writes.
2. Touch a doc-relevant file, commit, re-run ⇒ exactly that repo re-scouted; card regenerated to disk;
   `kb-atomize.py --only <a-project>` re-projects its atomics; a second `kb-atomize.py` run ⇒ zero git diff.
3. `py -3 -m pytest tests/kb-sync/test_atomize.py` (from the plugin repo) ⇒ all green (atomizer contract).
