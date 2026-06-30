---
name: kb-status
description: Show a comprehensive, offline status view of the knowledge base. Invoke when the user says "kb status", "knowledge base status", "what's stale in the KB", "show kb health", "/kb-status", or "how many projects need syncing".
---

<!-- Implementation: ../kb-sync/kb-status.py (skill dir is here for discovery; .py lives alongside kb-staleness.py in kb-sync/) -->

# kb-status — KB operator status view

Offline, read-only, no-LLM command that produces a comprehensive operator view of
the knowledge base in a single pass.

## What it shows

1. **Per-project freshness table** — every project card with its staleness state
   (`fresh` / `stale` / `very_stale` / `unknown`), commit drift count, and age in
   days. Sorted worst-first (`very_stale` > `stale` > `unknown` > `fresh`).

2. **Roll-up summary** — counts per state, total projects, and how many projects
   need a `/kb-sync` run (stale + very_stale; unknown is excluded from the sync count).

3. **Graph health** — dangling edge count, low-confidence edge count, and node/edge
   totals from `00-meta/lineage-quality.json`. Gracefully degrades if the file is
   missing (shows the freshness table + summary, notes the missing file).

4. **Mission Control board pointer** — link to the Astro-based KB board
   (default `http://localhost:4321`; change the `BOARD_URL` constant in
   `kb-status.py` to point to the LAN address when the board moves).

## Properties

- **Read-only** — makes no writes to the vault or any repo.
- **Offline** — no network calls, no LLM, no external dependencies (stdlib only).
- **Hermetic staleness source** — delegates all git staleness logic to
  `kb-staleness.py` (the shared engine); no duplicate git logic.

## Commands

Human-readable table (default):
```
cd ${CLAUDE_PLUGIN_ROOT}/skills/kb-sync
py -3 kb-status.py --vault <vault>
```

Machine-readable JSON (for scripting or other tooling):
```
cd ${CLAUDE_PLUGIN_ROOT}/skills/kb-sync
py -3 kb-status.py --vault <vault> --json
```

## Relationship to lightweight staleness checks

Quick per-project staleness queries (e.g. `py -3 kb-staleness.py --vault <vault>`)
give a fast single-metric view. This command is the **comprehensive complement**:
run it when you need the full picture — all projects sorted worst-first, graph
health, exact drift counts — before deciding whether to invoke `/kb-sync`.

## Tests

```
cd ${CLAUDE_PLUGIN_ROOT}/skills/kb-sync
py -3 -m pytest tests/test_kb_status.py -v
```
