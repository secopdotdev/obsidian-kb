---
description: Re-document changed repos into the knowledge base (invokes the kb-sync skill).
---

# /kb-sync

Thin wrapper — the full procedure lives in the `kb-sync` skill. This command loads that
skill and delegates.

## Prerequisites

Before invoking, confirm:

- `KB_VAULT` is set to the absolute path of your knowledge-base vault (the git repo
  scaffolded by `/kb-init`).
- `KB_DEV_ROOT` is set to the directory that **contains** your source repos (the parent
  of the group folders, e.g. `~/repos` or `D:\repos`). This is required — `kb-sync` throws
  if neither `KB_DEV_ROOT` nor a fallback is available.
- The skill exists at `${CLAUDE_PLUGIN_ROOT}/skills/kb-sync/` (it ships with the plugin).

## Invocation

Load and follow the skill:

```
${CLAUDE_PLUGIN_ROOT}/skills/kb-sync/SKILL.md
```

Pass any user-supplied flags (e.g. `--repo <name>`, `--all`) through to the skill's
orchestrator as described in the skill's **Workflow args contract**.

The skill owns the full multi-phase pipeline (change detection → harvest → scouts →
synthesis → atomize → index → commit). Do not duplicate its logic here.
