---
name: kb-patch
description: Targeted single-project KB update — use when one repo changes (credential added, feature shipped, config updated) without needing a full rebuild. Runs the full kb-sync pipeline scoped to one project. Invoke when user says "patch the KB for X", "update the kb card for my-tool", "kb-patch <project>", or "just sync X in the KB".
---

# kb-patch — targeted single-project KB update

Scoped to one repo. Costs ~$0.04 (one Haiku scout + one Sonnet synth card write) vs
~$1.10 for `--all`. Idempotent: an unchanged HEAD exits after Phase 1 at $0.00.

## When to use vs. full /kb-sync

| Trigger | Command |
|---------|---------|
| One project changed | `/kb-patch <project-name>` (this skill) |
| Credential / key added to my-tool | `/kb-patch my-tool` |
| New docs/kb/ added to a project | `/kb-patch <project-name>` |
| 3+ projects changed | `/kb-sync` (standard full flow) |
| Weekly maintenance | `/kb-sync` + kb-change-detect filters stale set |

## Environment

```bash
export KB_ROOT=~/repos/kb          # vault path
export KB_DEV_ROOT=~/repos                        # dev root (parent of tier dirs)
export CLAUDE_PLUGIN_ROOT=~/.claude              # plugin root (kb-sync scripts live here)
```

## Phase 0 — identify project

If the user provides a project name, use it directly. Otherwise infer from `$PWD`:
```bash
# Infer project name from current working directory
basename "$PWD"   # e.g. "my-tool" if cwd is ~/repos/my-tool
```

The project name must match the `name` field in `$KB_ROOT/00-meta/kb-manifest.json`.
To list all tracked project names:
```bash
python3 -c "import json; [print(p['name']) for p in json.load(open('$KB_ROOT/00-meta/kb-manifest.json'))]"
```

## Phase 1 — change detection (free, ~0 tokens)

```bash
python3 $CLAUDE_PLUGIN_ROOT/skills/kb-sync/kb-change-detect.py \
  --vault "$KB_ROOT" \
  --repo <project-name>
```

**If output is empty JSON array `[]`:** repo is unchanged (HEAD SHA matches `last_documented_sha`
in the manifest). Report "already current — no sync needed" and stop. **Cost: $0.00.**

**If output contains the project object:** proceed to Phase 2.

## Phase 2A — deterministic harvest (free, no LLM)

```bash
python3 $CLAUDE_PLUGIN_ROOT/skills/kb-sync/kb-harvest.py \
  --path <repo-absolute-path> \
  --name <project-name> \
  --group <group> \
  --head-sha <head_sha_from_phase1> \
  --out "$KB_ROOT/00-meta/scout-cache/<project-name>.json"
```

`group` and `repo-absolute-path` come from the Phase 1 JSON output.

## Phase 2B + 3 — Haiku scout + Sonnet synth (~$0.04)

Invoke the `kb-sync` Workflow tool with a single-repo `repos` array (all fields from Phase 1 output):

```json
{
  "vault": "<KB_ROOT>",
  "scoutPrompt": "<CLAUDE_PLUGIN_ROOT>/skills/kb-sync/scout-prompt.md",
  "template": "<KB_ROOT>/_templates/project.md",
  "buildAggregates": false,
  "now": "<ISO-8601 UTC>",
  "repos": [
    {
      "path": "<repo-absolute-path>",
      "path_rel": "<group>/<project-name>",
      "name": "<project-name>",
      "group": "<group>",
      "head_sha": "<head_sha>",
      "changed_files": [],
      "last_documented_sha": "<prev_sha>"
    }
  ]
}
```

`buildAggregates: false` skips the `_INDEX` and `llms.txt` regeneration (those only need
rebuilding on full syncs). The workflow writes the card + merged scout-cache to disk.

## Phase 4 — atomic notes + index (free, no LLM)

```bash
# Project atomics only
python3 $CLAUDE_PLUGIN_ROOT/skills/kb-sync/kb-atomize.py \
  --cache "$KB_ROOT/00-meta/scout-cache" \
  --vault "$KB_ROOT" \
  --date $(date +%Y-%m-%d) \
  --only <project-name>

# Rebuild SQLite index (gitignored; always rebuild after card changes)
python3 $CLAUDE_PLUGIN_ROOT/skills/kb-sync/kb-index.py \
  --vault "$KB_ROOT" \
  --out "$KB_ROOT/00-meta/kb.sqlite"
```

## Phase 5 — commit vault

```bash
cd "$KB_ROOT"
git add 02-projects/ 00-meta/scout-cache/ 03-adr/ 04-cli-errors/
git commit -m "chore(kb): sync <project-name> card [kb-patch]"
git push
```

## Incremental pattern: credential added to my-tool

When `my-tool set <new.key> <value>` adds a key that represents a **new capability or project**
documented in `my-tool/docs/kb/_meta.md`:

1. Edit `my-tool/docs/kb/_meta.md` — add the new tag (e.g., `"capability/projgamma"`) and update
   the description if the credential covers a new integration.
2. `git add docs/kb/_meta.md && git commit -m "docs(kb): add projgamma credential tag"` in the
   `my-tool` repo.
3. Run `/kb-patch my-tool` — Phase 1 detects the commit, Haiku re-scouts the repo, Sonnet
   updates the vault card.

If the key is just another entry under an existing capability (no new integration, no new tag),
**skip the sync entirely** — the vault card is already accurate. my-tool keys live in DPAPI (not
git), so they produce no SHA drift. Only `docs/kb/` edits drive the sync.

## Cost summary

| Path | Models | Est. cost |
|------|--------|-----------|
| Unchanged HEAD (Phase 1 exits) | none | $0.00 |
| One repo, changed | Haiku + Sonnet | ~$0.04 |
| my-tool (no docs/kb change) | none | $0.00 |

## Invariants

- Never passes `--all` — this skill is scoped to one repo only.
- `buildAggregates: false` prevents clobbering `llms.txt` / full `_INDEX` during a patch.
- Operator-owned fields (`rag-flag`, `notes`, `blockers`) are round-tripped, not overwritten.
