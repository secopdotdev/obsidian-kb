# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - unreleased

Initial public release — Tier 1 ("clean packaging").

### Added

- **Manifests** — `.claude-plugin/plugin.json` + single-repo `marketplace.json` (`source: "."`).
- **Skills (runtime)** — `kb-sync` (harvest → atomize → index → graph pipeline) and `kb-status`
  (offline freshness + graph-health report), ported and genericized from the source toolkit.
- **Commands (runtime)** — `/kb-init` (scaffold a vault; idempotent only-if-absent copy with
  STAGE→PAUSE→SUBMIT; optional `--with-obsidian` / `--with-board`), plus `/kb-sync` and
  `/kb-status` wrappers.
- **Hooks (runtime)** — `SessionStart` status + prerequisite check, `kb-enforce` structure-nudge
  (offers `/kb-init` in KB-less repos; `.kb-ignore` / `KB_ENFORCE=0` opt-out; writes nothing),
  and a commit nudge. Paths resolve via `${CLAUDE_PLUGIN_ROOT}` (cross-OS clean).
- **Scaffold — Core** (`/kb-init`, always) — reconciler package (rename/relocate/retire/absorb +
  stable `kb_id` identity + ledger), note `_templates/`, conventions + tag taxonomy, folder
  skeleton with generated-index seeds, `CLAUDE.md` navigation doctrine, `llms.txt` router.
- **Scaffold — optional viewers** — Obsidian config skeleton + example `Home.md`
  (`--with-obsidian`); Astro "Mission Control" board source (`--with-board`).
- **Cross-machine distribution** — `extraKnownMarketplaces` + `enabledPlugins` settings recipe
  and a documented `managed-settings.json` enforcement path (README).
- **CI** — pytest (reconciler + kb-sync suites), Node syntax checks, and manifest validation.

### Security

- Full secret/PII/employer-data sanitization pass before first commit: the employer-specific
  group slug, homelab hostnames, personal identifiers, and real project names replaced with
  generic placeholders across all shipped code, scaffold seeds, and test fixtures; generated
  artifacts (`backlinks.json`, real `Home.md`, Obsidian workspace state) excluded from the bundle.

[0.1.0]: https://github.com/secopdotdev/obsidian-kb/releases/tag/v0.1.0
