# reconciler

Durable, repo-resident reconciliation for KB project **lifecycle** events — rename,
relocate, retire, absorb — plus a stable per-project identity (`kb_id`) and offline
drift **discovery**. Lifecycle changes are never one-off hand edits; they are recorded
in an append-only ledger and applied idempotently.

- **Spec of record:** `active/plan/reconciler/00-spec.md` (ops) + `02-spec-kb-id.md` (identity/detect).
- **Decisions:** ADR-0007 (reconciler + ledger own lifecycle reconciliation), ADR-0008 (stable `kb_id` identity).

## Mental model

| Concept | Where it lives | Role |
|---|---|---|
| **`.kb-id`** (uuid4) | root file in each project repo | **single source of truth** for identity; survives rename/relocate/repo-rename |
| `name` (dir basename) | filesystem / card stem / scout-cache / edges | **mutable slug** — derived, re-keyable |
| **ledger** | `reconcile/ledger.yaml` (append-only) | history of rename/relocate/retire/absorb ops |
| **identity baseline** | `reconcile/identity.yaml` (generated snapshot) | last-reconciled `kb_id → {name, group}`; what `detect` diffs against |

`kb_id` is the invariant; `name` is the slug. A rename is "same `kb_id`, new `name`" — which
is why the reconciler can tell a rename from a death + rebirth. The `.kb-id` files are the
source of truth; `reconcile/identity.yaml` is a deterministic snapshot the reconciler owns.

## Commands

All run from the repo root: `py -3 tools/reconciler/reconciler.py <cmd> [--vault .] [--ledger reconcile/ledger.yaml]`.

| Command | What it does |
|---|---|
| `status` | Print ledger entries + convergence state. |
| `apply` | **Dry-run by default** — compute the mutation plan vs the vault (`N planned mutations`). `--commit` writes the mutations, prints the reproject follow-up, and refreshes `identity.yaml`. |
| `record <op> …` | Append a new op to the ledger (`retire --owner`, `relocate --name --to --group`, `rename --old --new --to [--repo]`, `absorb --from --into --subpath`). Date via `--date` or `KB_DATE`. |
| `stamp [--commit] [--only NAME …]` | Backfill `.kb-id`. **Dry-run** previews per repo (`would-write` / `exists` / `skipped-dirty` / `missing-repo`). `--commit` writes `.kb-id` **only-if-absent** and commits on each repo's **current branch only if clean** — dirty repos are skipped (stamp them manually), and it **never pushes**. Then refreshes `identity.yaml`. |
| `detect [--json]` | **Offline** drift discovery. Diffs the live `.kb-id` scan against `identity.yaml` and **proposes** rename/relocate/retire/new ops (+ reports collisions/unstamped). **Never writes the ledger** — you confirm a proposal, then `record` + `apply`. |

### Typical flows

**Backfill identity (one-time):**
```
py -3 tools/reconciler/reconciler.py stamp            # dry-run preview
py -3 tools/reconciler/reconciler.py stamp --commit   # mint + commit .kb-id (clean repos), refresh baseline
# then push the .kb-id commits in each stamped repo yourself
```

**Discover + reconcile drift (after a dir rename / move / removal):**
```
py -3 tools/reconciler/reconciler.py detect           # see proposed ops
py -3 tools/reconciler/reconciler.py record rename --old X --new Y --to grp/Y --date 2026-06-22
py -3 tools/reconciler/reconciler.py apply --commit   # migrate vault surfaces + refresh baseline
# then reproject (apply prints the exact commands):
py -3 "$KB_SKILL_DIR/kb-atomize.py" --cache 00-meta/scout-cache --vault . --date 2026-06-22
py -3 "$KB_SKILL_DIR/kb-index.py"   --vault . --out 00-meta/kb.sqlite
```

## Safety invariants

- **STAGE → PAUSE → SUBMIT.** `apply`/`stamp` are dry-run by default; `--commit` is the explicit submit. `detect` only proposes.
- **`stamp` never pushes** and never touches a dirty tree; on a failed git add/commit it rolls back the partial `.kb-id`.
- **Idempotent** — re-running `apply`/`stamp` after convergence is a no-op (`0 planned` / `exists`).
- **Append-only ledger** — corrections are new entries; name-keyed history stays valid (ops may carry an optional `id:`).
- **Fail-safe baseline** — a corrupt `identity.yaml` degrades to empty (never crashes); `refresh` preserves a non-empty baseline if the live scan returns zero (guards a mispointed `KB_DEV_ROOT`).

## Layout

```
tools/reconciler/
  reconciler.py   CLI (status/apply/record/stamp/detect)
  ledger.py       load/validate/append reconcile/ledger.yaml
  ops.py          apply_{retire,relocate,rename,absorb} + apply_all (id-aware)
  vault.py        atomic vault primitives (cards/manifest/edges/atomics/scout-cache)
  identity.py     .kb-id baseline: scan_live / load|write|refresh_baseline / resolve_name
  idgen.py        .kb-id mint/read/write/validate (uuid4)
  paths.py        dev_root() / resolve_repo() (KB_DEV_ROOT)
  detect.py       offline drift discovery → proposed ops
  tests/          hermetic pytest suite (mocked git / dev-root)
```

Run the suite: `py -3 -m pytest tools/reconciler/tests/ -v`.

## Notes / follow-ups

- `stamp` enumerates repos from the **manifest**; `detect` scans the **filesystem**. On-disk dirs
  not in the manifest read as `unstamped` (untracked — add via kb-sync intake, then stamp). Stale
  manifest entries with no on-disk repo read as `missing-repo`.
- The card `kb_id:` frontmatter is optional visibility; the reconciler keys on the `.kb-id` file
  and `identity.yaml`, not the card/manifest (the manifest has no wholesale generator — see ADR-0008).
