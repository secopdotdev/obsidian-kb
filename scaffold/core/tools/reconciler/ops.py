"""
Op handlers — apply ledger ops to the vault.

Each handler has the signature:
    def apply_<op>(vault: Path, op: dict, commit: bool) -> Plan

A handler MUST be idempotent: if the vault is already in the desired state,
it returns an empty Plan ([]).  When commit=False, the Plan is computed
identically but no files are written to disk.
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path
from typing import Any, Callable

from tools.reconciler import identity
from tools.reconciler import vault as vault_mod
from tools.reconciler.ledger import validate_op

# ---------------------------------------------------------------------------
# Shared types (other tasks import these verbatim)
# ---------------------------------------------------------------------------

Mutation = dict  # {"surface": str, "action": str, "path": str, "detail": str}
Plan = list[Mutation]


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


def apply_retire(vault: Path, op: dict[str, Any], commit: bool) -> Plan:
    """Ensure a retired project is removed from all vault surfaces.

    Surfaces (each appends a Mutation if work is needed):
      1. Delete the project card under 02-projects/**/<owner>.md
      2. Purge manifest entry in 00-meta/kb-manifest.json
      3. Remove inbound edge refs from 00-meta/project-edges.yaml
      4. Ensure owner present in 00-meta/retired-projects.txt
      5. Delete scout-cache entry at 00-meta/scout-cache/<owner>.json

    Returns an empty Plan when the vault is already converged.
    When commit=False the Plan is computed but nothing is written.
    """
    owner: str = identity.resolve_name(vault, op, key="owner") or op["owner"]
    plan: Plan = []

    # -- Surface 1: card(s) ---------------------------------------------------
    # Use find_cards (plural) so that duplicate cards left in multiple group
    # folders by a prior botched move are ALL deleted in one pass.
    for card_path in vault_mod.find_cards(vault, owner):
        plan.append(
            {
                "surface": "card",
                "action": "delete",
                "path": str(card_path.relative_to(vault)),
                "detail": f"Deleted retired project card for {owner!r}",
            }
        )
        if commit:
            vault_mod.delete_card(card_path)

    # -- Surface 2: manifest ---------------------------------------------------
    manifest = vault_mod.load_manifest(vault)
    new_manifest = [e for e in manifest if e.get("name") != owner]
    if len(new_manifest) < len(manifest):
        plan.append(
            {
                "surface": "manifest",
                "action": "purge",
                "path": "00-meta/kb-manifest.json",
                "detail": f"Removed manifest entry for {owner!r}",
            }
        )
        if commit:
            vault_mod.write_manifest(vault, new_manifest)

    # -- Surface 3: edges ------------------------------------------------------
    # Probe what would be removed without writing (read-only).
    _new_edges_data, removed_refs = vault_mod._compute_edges_remove(vault, owner)
    if removed_refs:
        plan.append(
            {
                "surface": "edges",
                "action": "remove-refs",
                "path": "00-meta/project-edges.yaml",
                "detail": f"Removed edge refs for {owner!r}: {removed_refs}",
            }
        )
        if commit:
            vault_mod.rewrite_edges_remove(vault, owner)

    # -- Surface 4: retired-projects.txt ---------------------------------------
    existing_owners = vault_mod._load_retired_owners(vault)
    if owner not in existing_owners:
        new_owners = existing_owners | {owner}
        plan.append(
            {
                "surface": "retired-projects",
                "action": "add",
                "path": "00-meta/retired-projects.txt",
                "detail": f"Added {owner!r} to retired-projects.txt",
            }
        )
        if commit:
            vault_mod.regen_retired_projects(vault, new_owners)

    # -- Surface 5: scout-cache -----------------------------------------------
    scout_path = vault / "00-meta" / "scout-cache" / f"{owner}.json"
    if scout_path.exists():
        plan.append(
            {
                "surface": "scout-cache",
                "action": "delete",
                "path": f"00-meta/scout-cache/{owner}.json",
                "detail": f"Deleted scout-cache entry for {owner!r}",
            }
        )
        if commit:
            vault_mod.delete_scout_cache(vault, owner)

    return plan


# ---------------------------------------------------------------------------
# relocate
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the parsed frontmatter dict from a card's raw text."""
    lines = text.splitlines(keepends=False)
    fm_start: int | None = None
    fm_end: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if fm_start is None:
                fm_start = i
            else:
                fm_end = i
                break
    if fm_start is None or fm_end is None:
        return {}
    block = "\n".join(lines[fm_start + 1 : fm_end])
    result = yaml.safe_load(block)
    return result if isinstance(result, dict) else {}


def apply_relocate(vault: Path, op: dict[str, Any], commit: bool) -> Plan:
    """Ensure a project's card and manifest reflect a new group/path location.

    Surfaces (each appends a Mutation when work is needed):
      1. Set card frontmatter ``path:`` = op["to"].
      2. If op["group"] differs from the card's current group: update
         frontmatter ``group:`` and the ``group/<old>`` tag → ``group/<new>``
         (frontmatter block only — body tags/prose are NOT rewritten);
         then MOVE the card file to ``02-projects/<new-group>/<name>.md``.
      3. Manifest entry: set ``group``, ``card_path``, and ``path`` (if present).
      4. (cleanup) Any duplicate same-name card at a path other than dest is
         deleted (one Mutation each).

    Returns an empty Plan when the vault is already converged (idempotent).
    When commit=False the Plan is computed but nothing is written to disk.
    """
    name: str = identity.resolve_name(vault, op, key="name") or op["name"]
    new_path: str = op["to"]
    new_group: str = op["group"]

    # FIX 4: Cross-validate op["group"] against the path segment in op["to"].
    path_group = vault_mod.group_of_path(new_path)
    if new_group and path_group and path_group != new_group:
        raise ValueError(
            f"relocate: group {new_group!r} disagrees with path {new_path!r}"
        )

    # FIX 3: Use find_cards (plural) to handle duplicates from botched prior moves.
    all_cards = vault_mod.find_cards(vault, name)
    if not all_cards:
        raise FileNotFoundError(
            f"No card found for project {name!r} in {vault / '02-projects'}"
        )

    # Compute expected destination card path.
    dest_card = vault / "02-projects" / new_group / f"{name}.md"

    # Identify the source card: prefer the card already at dest (already moved)
    # so that a partially-applied prior run is idempotent.
    source_card: Path = dest_card if dest_card in all_cards else all_cards[0]

    card_text = open(source_card, encoding="utf-8", newline="").read()
    fm = _parse_frontmatter(card_text)
    current_path: str = str(fm.get("path", ""))
    current_group: str = str(fm.get("group", ""))

    # Check per-surface whether work is needed.
    path_needs_update = current_path != new_path
    group_needs_update = current_group != new_group
    # Key the physical move on whether the source card is already at the destination.
    card_needs_move = source_card.resolve() != dest_card.resolve()

    # Identify duplicate cards that must be cleaned up (all cards != dest).
    duplicate_cards = [c for c in all_cards if c.resolve() != dest_card.resolve() and c.resolve() != source_card.resolve()]

    # Manifest check.
    manifest = vault_mod.load_manifest(vault)
    man_entry: dict[str, Any] | None = next(
        (e for e in manifest if e.get("name") == name), None
    )
    manifest_needs_update = man_entry is not None and (
        man_entry.get("group") != new_group
        or man_entry.get("card_path") != f"02-projects/{new_group}/{name}.md"
        or ("path" in man_entry and man_entry.get("path") != new_path)
    )

    if (
        not path_needs_update
        and not group_needs_update
        and not card_needs_move
        and not manifest_needs_update
        and not duplicate_cards
    ):
        return []

    plan: Plan = []

    # -- Surface 1: card path field ------------------------------------------
    if path_needs_update:
        plan.append(
            {
                "surface": "card",
                "action": "set-path",
                "path": str(source_card.relative_to(vault)),
                "detail": f"Set path: {new_path!r} in card for {name!r}",
            }
        )
        if commit:
            vault_mod.set_frontmatter_field(source_card, "path", new_path)

    # -- Surface 2: card group + tag (frontmatter only) + file move -----------
    if group_needs_update or card_needs_move:
        plan.append(
            {
                "surface": "card",
                "action": "move-group",
                "path": str(dest_card.relative_to(vault)),
                "detail": (
                    f"Moved card for {name!r} from group {current_group!r} "
                    f"to {new_group!r}"
                ),
            }
        )
        if commit:
            if group_needs_update:
                # Update group: field (uses set_frontmatter_field which preserves endings).
                vault_mod.set_frontmatter_field(source_card, "group", new_group)
                # FIX 1: Rewrite group/<old> → group/<new> in frontmatter ONLY.
                # Re-read with newline="" to preserve CRLF.
                updated_text = open(source_card, encoding="utf-8", newline="").read()
                if current_group:
                    lines = updated_text.splitlines(keepends=True)
                    in_fm = False
                    closed = False
                    for i, ln in enumerate(lines):
                        if ln.strip() == "---":
                            if not in_fm:
                                in_fm = True
                            else:
                                closed = True
                                break
                        elif in_fm and not closed:
                            lines[i] = ln.replace(
                                f"group/{current_group}", f"group/{new_group}", 1
                            )
                    updated_text = "".join(lines)
                vault_mod.atomic_write(source_card, updated_text)
            if card_needs_move:
                # Move the file to its expected destination folder.
                vault_mod.move_card(source_card, dest_card)

    # -- Surface 2b: duplicate cleanup -----------------------------------------
    for dup in duplicate_cards:
        plan.append(
            {
                "surface": "card",
                "action": "delete-duplicate",
                "path": str(dup.relative_to(vault)),
                "detail": (
                    f"Deleted duplicate card for {name!r} at "
                    f"{dup.relative_to(vault)}"
                ),
            }
        )
        if commit:
            vault_mod.delete_card(dup)

    # -- Surface 3: manifest ---------------------------------------------------
    if manifest_needs_update:
        plan.append(
            {
                "surface": "manifest",
                "action": "update",
                "path": "00-meta/kb-manifest.json",
                "detail": (
                    f"Updated manifest entry for {name!r}: group={new_group!r}, "
                    f"card_path=02-projects/{new_group}/{name}.md"
                ),
            }
        )
        if commit:
            new_manifest = []
            for e in manifest:
                if e.get("name") == name:
                    e = dict(e)
                    e["group"] = new_group
                    e["card_path"] = f"02-projects/{new_group}/{name}.md"
                    if "path" in e:
                        e["path"] = new_path
                new_manifest.append(e)
            vault_mod.write_manifest(vault, new_manifest)

    return plan


# ---------------------------------------------------------------------------
# absorb
# ---------------------------------------------------------------------------


def apply_absorb(vault: Path, op: dict[str, Any], commit: bool) -> Plan:
    """Consolidate a project (from) into a host project (into).

    Surfaces (each appends a Mutation when work is needed):
      1. CARDS   — delete all cards for <from> under 02-projects/.
      2. SCOUT   — delete 00-meta/scout-cache/<from>.json.
      3. MANIFEST— purge manifest entry where name==<from>.
      4. ATOMICS — delete atomic notes owned by <from> (tool: filter guards siblings).
      5. EDGES   — repoint every reference from <from> → <into>, deduping.
      6. PROVENANCE — append "<from> -> <subpath>" to the host card's absorbed: list.

    Idempotent: if <from> is fully gone AND the provenance entry is already
    present on the host card, returns [].
    When commit=False the Plan is computed but nothing is written.
    """
    from_: str = identity.resolve_name(vault, op, key="from") or op["from"]
    into: str = op["into"]
    subpath: str = op["subpath"]
    provenance_value = f"{from_} -> {subpath}"

    # Fail fast: the host card must exist before any surface mutates, so absorb
    # never leaves a partial apply (from retired but provenance unrecordable).
    if not vault_mod.find_cards(vault, into):
        raise ValueError(
            f"absorb: host card for {into!r} not found; cannot record provenance"
        )

    plan: Plan = []

    # -- Surface 1: cards for <from> ------------------------------------------
    for card_path in vault_mod.find_cards(vault, from_):
        plan.append(
            {
                "surface": "card",
                "action": "delete",
                "path": str(card_path.relative_to(vault)),
                "detail": f"Deleted absorbed project card for {from_!r}",
            }
        )
        if commit:
            vault_mod.delete_card(card_path)

    # -- Surface 2: scout-cache -----------------------------------------------
    scout_path = vault / "00-meta" / "scout-cache" / f"{from_}.json"
    if scout_path.exists():
        plan.append(
            {
                "surface": "scout-cache",
                "action": "delete",
                "path": f"00-meta/scout-cache/{from_}.json",
                "detail": f"Deleted scout-cache entry for absorbed {from_!r}",
            }
        )
        if commit:
            vault_mod.delete_scout_cache(vault, from_)

    # -- Surface 3: manifest ---------------------------------------------------
    manifest = vault_mod.load_manifest(vault)
    new_manifest = [e for e in manifest if e.get("name") != from_]
    if len(new_manifest) < len(manifest):
        plan.append(
            {
                "surface": "manifest",
                "action": "purge",
                "path": "00-meta/kb-manifest.json",
                "detail": f"Removed manifest entry for absorbed {from_!r}",
            }
        )
        if commit:
            vault_mod.write_manifest(vault, new_manifest)

    # -- Surface 4: atomics owned by <from> -----------------------------------
    owned_atomics = vault_mod._find_owned_atomics(vault, from_)
    if owned_atomics:
        plan.append(
            {
                "surface": "atomics",
                "action": "delete",
                "path": "04-cli-errors/ 03-adr/ 08-blockers/",
                "detail": (
                    f"Deleted {len(owned_atomics)} atomic note(s) owned by {from_!r}"
                ),
            }
        )
        if commit:
            vault_mod.delete_atomics(vault, from_)

    # -- Surface 5: edges repoint <from> → <into> -----------------------------
    edges_path = vault / "00-meta" / "project-edges.yaml"
    edges_raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    edges_data: dict[str, Any] = yaml.safe_load(edges_raw) or {}
    edges_has_from = isinstance(edges_data, dict) and (
        from_ in edges_data
        or any(
            from_ in list_val
            for v in edges_data.values()
            if isinstance(v, dict)
            for list_val in v.values()
            if isinstance(list_val, list)
        )
    )
    if edges_has_from:
        plan.append(
            {
                "surface": "edges",
                "action": "repoint-refs",
                "path": "00-meta/project-edges.yaml",
                "detail": f"Repointed edge refs {from_!r} → {into!r}",
            }
        )
        if commit:
            vault_mod.rewrite_edges_repoint(vault, from_, into)

    # -- Surface 6: provenance on host card -----------------------------------
    # Find the host (into) card and check whether provenance is already present.
    host_cards = vault_mod.find_cards(vault, into)
    if host_cards:
        host_card = host_cards[0]
        # Read current frontmatter to probe existing absorbed list.
        host_text = open(host_card, encoding="utf-8", newline="").read()
        fm = _parse_frontmatter(host_text)
        absorbed_list = fm.get("absorbed", [])
        if not isinstance(absorbed_list, list):
            absorbed_list = []
        if provenance_value not in absorbed_list:
            plan.append(
                {
                    "surface": "provenance",
                    "action": "append",
                    "path": str(host_card.relative_to(vault)),
                    "detail": (
                        f"Recorded absorbed: {provenance_value!r} on host {into!r}"
                    ),
                }
            )
            if commit:
                vault_mod.append_frontmatter_list_item(
                    host_card, "absorbed", provenance_value
                )

    return plan


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def apply_rename(vault: Path, op: dict[str, Any], commit: bool) -> Plan:
    """Migrate EVERY surface keyed on op["old"] to op["new"].

    Surfaces (each appends a Mutation when work is detected):
      1. CARD: move 02-projects/<group>/<old>.md → <new>.md; rewrite title/path/
         repo in frontmatter; add <old> as alias; fix tool/<old> → tool/<new> tag.
      2. SCOUT-CACHE: move 00-meta/scout-cache/<old>.json → <new>.json; update
         identity.name field.
      3. MANIFEST: rename the entry where name==<old>.
      4. ATOMICS: rename cmd-/err-/adr-/blk- files; rewrite tool tags + wikilinks
         inside each.
      5. WIKILINKS: rewrite [[<old>…]] → [[<new>…]] across all *.md.
      6. EDGES: rename top-level key and list-item refs in project-edges.yaml.

    Idempotent: if <old> is fully gone and only <new> exists → returns [].
    When commit=False the Plan is computed but nothing is written to disk.
    """
    # rename's `old` is a HISTORICAL slug and is explicit in the op — it must NOT be
    # resolved through the mutable identity baseline. After apply+refresh the baseline maps
    # id->new, so resolving `old` via id would return `new` and make a 2nd apply self-rename
    # (non-idempotent). `new` and the other ops resolve via id; rename's `old` never does.
    old: str = op["old"]
    new: str = op["new"]
    new_to: str | None = op.get("to")
    new_repo: str | None = op.get("repo")

    plan: Plan = []

    # ── Surface 1: CARD ──────────────────────────────────────────────────────
    old_cards = vault_mod.find_cards(vault, old)

    # Resolve the destination group: op["to"] wins; else the current card folder;
    # else (no old card) an existing <new> card's folder.
    if new_to:
        dest_group = vault_mod.group_of_path(new_to)
    elif old_cards:
        dest_group = old_cards[0].parent.name
    else:
        _existing_new = vault_mod.find_cards(vault, new)
        dest_group = _existing_new[0].parent.name if _existing_new else ""
    dest_card = vault / "02-projects" / dest_group / f"{new}.md"

    _tag_re = re.compile(re.escape(f"tool/{old}") + r"(?=[\"'\s,\]}])")

    def _fix_fm_tool_tag(card: Path) -> None:
        """Rewrite tool/<old> → tool/<new> within the FIRST frontmatter block only.

        Boundary-anchored so a sibling tag (tool/<old>-x) is never corrupted.
        """
        text = open(card, encoding="utf-8", newline="").read()
        lines = text.splitlines(keepends=True)
        in_fm = closed = False
        for i, ln in enumerate(lines):
            if ln.strip() == "---":
                if not in_fm:
                    in_fm = True
                else:
                    closed = True
                    break
            elif in_fm and not closed:
                lines[i] = _tag_re.sub(f"tool/{new}", ln)
        vault_mod.atomic_write(card, "".join(lines))

    if old_cards:
        source_card = old_cards[0]
        plan.append(
            {
                "surface": "card",
                "action": "rename",
                "path": str(dest_card.relative_to(vault)),
                "detail": f"Renamed card {old!r} → {new!r}",
            }
        )
        if commit:
            vault_mod.move_card(source_card, dest_card)
            vault_mod.set_frontmatter_field(dest_card, "title", f'"{new}"')
            if new_to:
                vault_mod.set_frontmatter_field(dest_card, "path", new_to)
            if new_repo:
                vault_mod.set_frontmatter_field(dest_card, "repo", f'"{new_repo}"')
            vault_mod.add_alias(dest_card, old)
            _fix_fm_tool_tag(dest_card)
        # Delete any remaining duplicate old-name cards (botched prior move).
        for dup in old_cards[1:]:
            plan.append(
                {
                    "surface": "card",
                    "action": "delete-duplicate",
                    "path": str(dup.relative_to(vault)),
                    "detail": f"Deleted duplicate old card {old!r}",
                }
            )
            if commit:
                vault_mod.delete_card(dup)
    elif dest_card.exists():
        # No old card, but a <new> card exists. Repair ONLY unambiguous partial
        # migration (a crash that left tool/<old> or title:"<old>" in the dest).
        # We deliberately do NOT add the <old> alias here: a freshly-created
        # <new> card legitimately lacks it, and the idempotency contract is
        # "old gone + new exists → []".
        text = open(dest_card, encoding="utf-8", newline="").read()
        parts = text.split("---", 2)
        fm_block = parts[1] if len(parts) >= 3 else ""
        stale_tag = _tag_re.search(fm_block) is not None
        stale_title = f'title: "{old}"' in fm_block
        if stale_tag or stale_title:
            plan.append(
                {
                    "surface": "card",
                    "action": "repair",
                    "path": str(dest_card.relative_to(vault)),
                    "detail": f"Repaired partial migration on {new!r} card",
                }
            )
            if commit:
                if stale_title:
                    vault_mod.set_frontmatter_field(dest_card, "title", f'"{new}"')
                if stale_tag:
                    _fix_fm_tool_tag(dest_card)

    # ── Surface 2: SCOUT-CACHE ───────────────────────────────────────────────
    scout_old = vault / "00-meta" / "scout-cache" / f"{old}.json"
    if scout_old.exists():
        plan.append(
            {
                "surface": "scout-cache",
                "action": "rename",
                "path": f"00-meta/scout-cache/{new}.json",
                "detail": f"Renamed scout-cache {old!r} → {new!r}",
            }
        )
        if commit:
            vault_mod.rename_scout_cache(vault, old, new)

    # ── Surface 3: MANIFEST ──────────────────────────────────────────────────
    manifest = vault_mod.load_manifest(vault)
    old_entry: dict[str, Any] | None = next(
        (e for e in manifest if e.get("name") == old), None
    )
    if old_entry is not None:
        dest_group_man = (
            vault_mod.group_of_path(new_to) if new_to
            else str(old_entry.get("group", ""))
        )
        plan.append(
            {
                "surface": "manifest",
                "action": "rename",
                "path": "00-meta/kb-manifest.json",
                "detail": f"Renamed manifest entry {old!r} → {new!r}",
            }
        )
        if commit:
            new_manifest: list[dict[str, Any]] = []
            for e in manifest:
                if e.get("name") == old:
                    e = dict(e)
                    e["name"] = new
                    e["card_path"] = f"02-projects/{dest_group_man}/{new}.md"
                    if "path" in e and new_to:
                        e["path"] = new_to
                new_manifest.append(e)
            vault_mod.write_manifest(vault, new_manifest)

    # ── Surface 4: ATOMICS ───────────────────────────────────────────────────
    # Probe: check whether any old-named atomics exist.
    atomic_patterns = [
        (vault / "04-cli-errors", f"cmd-{old}-*.md"),
        (vault / "04-cli-errors", f"err-{old}-*.md"),
        (vault / "03-adr", f"{old}-adr-*.md"),
        (vault / "08-blockers", f"blk-{old}-*.md"),
    ]
    old_atomics: list[Path] = []
    for folder, glob_pat in atomic_patterns:
        if folder.exists():
            old_atomics.extend(sorted(folder.glob(glob_pat)))

    if old_atomics:
        plan.append(
            {
                "surface": "atomics",
                "action": "delete",
                "path": "04-cli-errors/ 03-adr/ 08-blockers/",
                "detail": (
                    f"Deleted {len(old_atomics)} atomic note(s) owned by {old!r}; "
                    f"kb-atomize regenerates them under {new!r}"
                ),
            }
        )
        if commit:
            vault_mod.rename_atomics(vault, old, new)

    # ── Surface 5: WIKILINKS ─────────────────────────────────────────────────
    # Probe: scan all *.md for [[<old>...]] occurrences (read-only).
    wikilink_re = re.compile(r"\[\[" + re.escape(old) + r"(?=[\]|#])")
    wikilink_files = [
        md for md in vault.rglob("*.md")
        if wikilink_re.search(open(md, encoding="utf-8", newline="").read())
    ]
    if wikilink_files:
        plan.append(
            {
                "surface": "wikilinks",
                "action": "rewrite",
                "path": f"**/*.md ({len(wikilink_files)} files)",
                "detail": f"Rewrote [[{old}…]] → [[{new}…]] in {len(wikilink_files)} file(s)",
            }
        )
        if commit:
            vault_mod.rewrite_wikilinks(vault, old, new)

    # ── Surface 6: EDGES ─────────────────────────────────────────────────────
    edges_path = vault / "00-meta" / "project-edges.yaml"
    edges_raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    # Probe: does any reference to old exist as a key or list value?
    edges_data: dict[str, Any] = yaml.safe_load(edges_raw) or {}
    edges_has_old = isinstance(edges_data, dict) and (
        old in edges_data
        or any(
            old in list_val
            for v in edges_data.values()
            if isinstance(v, dict)
            for list_val in v.values()
            if isinstance(list_val, list)
        )
    )
    if edges_has_old:
        plan.append(
            {
                "surface": "edges",
                "action": "rename-refs",
                "path": "00-meta/project-edges.yaml",
                "detail": f"Renamed edge refs {old!r} → {new!r}",
            }
        )
        if commit:
            vault_mod.rewrite_edges_rename(vault, old, new)

    return plan


# ---------------------------------------------------------------------------
# Dispatch table + aggregate orchestrator
# ---------------------------------------------------------------------------

DISPATCH: dict[str, Callable[[Path, dict[str, Any], bool], Plan]] = {
    "retire": apply_retire,
    "relocate": apply_relocate,
    "rename": apply_rename,
    "absorb": apply_absorb,
}


def apply_all(vault: Path, ledger_ops: list[dict[str, Any]], commit: bool) -> Plan:
    """Replay every op in ledger_ops against vault idempotently.

    Execution order:
      1. For each op in order: validate_op then dispatch to the handler,
         extending an aggregate plan.
      2. After all handlers run: compute the retired-owner UNION from the
         ledger (retire.owner union absorb.from) merged with the current
         retired-projects.txt.  If the ledger set introduces new owners,
         append a "regen" Mutation and (on commit) call regen_retired_projects
         with the full union so absorbed owners land in retired-projects.txt.

    Returns the aggregate Plan (empty list when already fully converged).
    When commit=False the Plan is computed but nothing is written to disk.
    """
    aggregate: Plan = []

    for op in ledger_ops:
        validate_op(op)
        handler = DISPATCH.get(op["op"])
        if handler is None:  # KNOWN_OPS gained an op before DISPATCH did
            raise NotImplementedError(f"No handler registered for op {op['op']!r}")
        aggregate.extend(handler(vault, op, commit))

    # ------------------------------------------------------------------
    # Post-dispatch: ensure absorbed owners land in retired-projects.txt.
    # `retire` ops emit + write their OWN owners via apply_retire (Surface 4),
    # so the union block only CLAIMS absorb-from owners — this keeps the dry-run
    # and commit plan counts in agreement (no double-emission for retire owners),
    # while the committed regen still writes the FULL union so the file is the
    # ledger-driven source of truth.
    # ------------------------------------------------------------------
    # Resolve owners via the identity baseline (same as the handlers) so an op
    # carrying an `id` with a stale typed name tombstones the RESOLVED slug, not
    # the stale literal (spec 02 D3 — match-by-id).
    retire_owners = {
        identity.resolve_name(vault, op, key="owner") or op["owner"]
        for op in ledger_ops if op["op"] == "retire"
    }
    absorb_owners = {
        str(identity.resolve_name(vault, op, key="from") or op["from"])
        for op in ledger_ops if op["op"] == "absorb"
    }
    existing_owners = vault_mod._load_retired_owners(vault)

    absorb_new = absorb_owners - existing_owners - retire_owners
    if absorb_new:
        union = existing_owners | retire_owners | absorb_owners
        aggregate.append(
            {
                "surface": "retired-projects",
                "action": "regen",
                "path": "00-meta/retired-projects.txt",
                "detail": (
                    f"Recorded absorbed owner(s) in retired-projects.txt: {sorted(absorb_new)}"
                ),
            }
        )
        if commit:
            vault_mod.regen_retired_projects(vault, union)

    return aggregate
