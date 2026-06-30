---
description: Show knowledge-base freshness + graph health (invokes the kb-status skill).
---

# /kb-status

Thin wrapper — the full procedure lives in the `kb-status` skill. This command loads
that skill and delegates.

## Properties

- **Read-only** — makes no writes to the vault or any repo.
- **Offline** — no network calls, no LLM, stdlib only.

## Prerequisites

- `KB_VAULT` is set to the absolute path of your knowledge-base vault.
- The skill exists at `${CLAUDE_PLUGIN_ROOT}/skills/kb-status/SKILL.md` (it ships with
  the plugin). Its implementation is at
  `${CLAUDE_PLUGIN_ROOT}/skills/kb-sync/kb-status.py`.

## Invocation

Load and follow the skill:

```
${CLAUDE_PLUGIN_ROOT}/skills/kb-status/SKILL.md
```

The skill runs:

```bash
cd "${CLAUDE_PLUGIN_ROOT}/skills/kb-sync"
py -3 kb-status.py --vault "<KB_VAULT>"
```

Pass `--json` if machine-readable output is needed (e.g. for scripting). The skill
owns the full output contract (freshness table, roll-up summary, graph health, board
pointer) — do not duplicate it here.
