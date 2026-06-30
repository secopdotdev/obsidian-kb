# knowledge-base-repo

Processes for the Mission Control knowledge base: kb-sync engine (harvest → index → graph),
reconciler (lifecycle manager for rename/relocate/retire), and kb-context-pack (read-only RAG
retrieval). Companion to the private `knowledge-base` vault.

A Claude Code plugin that turns a collection of Git repositories into a queryable knowledge base.
It harvests project metadata into a markdown vault backed by SQLite, renders a graph, and
optionally serves the result through an Obsidian vault or a static Astro board.

---

## What it is

The plugin ships two distinct payloads:

**RUNTIME** — loaded on install into Claude Code:
- `kb-sync` and `kb-status` skills (vault harvest + status reporting)
- Slash commands (`/kb-init`, `/kb-sync`, `/kb-status`)
- `SessionStart` and commit hooks (via `hooks/hooks.json`)

**SCAFFOLD** — written into a target repository by `/kb-init`:
- Reconciler (`tools/reconciler/`) — lifecycle manager for rename / relocate / retire / absorb operations
- Obsidian config skeleton (`scaffold/obsidian/`) — minimal `app.json`, plugin list, CSS snippet; no workspace state or plugin data
- Astro board source (`scaffold/board/web/`) — static site rendering the vault as Mission Control
- Note templates (`scaffold/_templates/`) — Templater scaffolds per note type

### Three layers

| Layer | Always? | What it adds |
|---|---|---|
| Core | Yes | Markdown vault + SQLite index + graph JSON + reconciler |
| Obsidian viewer | Optional (`--with-obsidian`) | Opens the vault folder in Obsidian with the bundled config |
| Astro board viewer | Optional (`--with-board`) | Static site served locally; `npm run dev` inside `web/` |

---

## Prerequisites

- **Python 3.13+** — Core layer (reconciler, harvest scripts, SQLite index)
- **Node 24+** — Astro board (`--with-board`) and hook scripts

Optional viewer prerequisites:
- **Obsidian** + community plugins (Dataview, Templater, Bases) — for the Obsidian viewer layer
- **Node toolchain** (already covered by Node 24+ above) — for `npm run dev` inside `web/`

---

## Install

### From the marketplace

```
/plugin marketplace add secopdotdev/obsidian-kb
/plugin install obsidian-kb@obsidian-kb
```

### Local development

```
/plugin marketplace add ./obsidian-kb
/plugin install obsidian-kb@obsidian-kb
```

---

## Quick start

```
# Scaffold a new knowledge-base into <target-repo>
/kb-init <target-repo> [--with-obsidian] [--with-board]

# Harvest all tracked repos into the vault
/kb-sync

# Report vault health + staleness
/kb-status
```

`/kb-init` writes the SCAFFOLD payload into `<target-repo>`, sets `KB_VAULT` in your environment, and runs an initial harvest. Pass `--with-obsidian` to open the vault in Obsidian; pass `--with-board` to scaffold the Astro board (requires Node 24+).

---

## Cross-machine distribution

To make the plugin — and its `SessionStart` enforcement hook — apply by default across
several machines, sync two settings keys and run one install per machine.

In a git-synced `~/.claude/settings.json` (or a project `.claude/settings.json`):

```jsonc
{
  "extraKnownMarketplaces": {
    "obsidian-kb": {
      "source": { "source": "github", "repo": "secopdotdev/obsidian-kb" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "obsidian-kb@obsidian-kb": true }
}
```

Then, once per machine:

```
/plugin install obsidian-kb@obsidian-kb
```

Plugin hooks and skills become available machine-wide once the plugin is enabled, and
plugin hooks resolve via `${CLAUDE_PLUGIN_ROOT}` — so they are cross-OS clean, unlike
hand-rolled `settings.json` hooks that hardcode an interpreter path.

### What distribution does *not* carry

A plugin cannot ship a global `~/.claude/CLAUDE.md`. So:

- **Global operating doctrine** still rides your dotfiles sync (or, for non-overridable
  enforcement, the managed-settings `claudeMd` field — see below).
- **Per-project KB doctrine** reaches a repo via the `CLAUDE.md` that `/kb-init` scaffolds
  into the vault (`scaffold/core/CLAUDE.md`).

### Non-overridable enforcement (optional, admin)

For a fleet you control, a managed `managed-settings.json`
(`/etc/claude-code/managed-settings.json` on Linux, `C:\Program Files\ClaudeCode\managed-settings.json`
on Windows) can force-enable the plugin (the `enabledPlugins` *array* form) and inject an
org `claudeMd` users cannot disable. This is a Tier-2 / admin option, not required for
personal use.

---

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `KB_VAULT` | Absolute path to the vault root | Required; set by `/kb-init` |
| `KB_DEV_ROOT` | Root directory scanned for repos | Required on first sync |
| `KB_BOARD_URL` | Local URL of the Astro board dev server | `http://localhost:4321` |

---

## Roadmap (Tier 2)

The following capabilities are not yet implemented and are planned for a future release:

- **Pluggable non-Obsidian writer** — alternative markdown renderers / export targets
- **Configurable taxonomy** — group slugs and tag taxonomy editable via config rather than hard-coded defaults
- **Cross-platform secrets** — currently relies on the host platform's secret store; a host-agnostic vault on-ramp is planned

---

## License

MIT. See [LICENSE](LICENSE).
