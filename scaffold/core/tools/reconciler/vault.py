"""
Vault primitives — atomic writes and vault-topology helpers.

Only atomic_write and detect_snapshot are implemented in Task 1.
Additional mutation primitives (card/manifest/atomics/edges/wikilinks)
will be added in subsequent tasks.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml


def atomic_write(path: Path | str, text: str) -> None:
    """Write text to path atomically via a temp file + os.replace.

    The temp file is created in the same directory as path so that
    os.replace is always on the same filesystem (no cross-device move).
    """
    path = Path(path)
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def load_manifest(vault: Path) -> list[dict[str, Any]]:
    """Load and return the parsed kb-manifest.json as a list of dicts."""
    path = vault / "00-meta" / "kb-manifest.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return data


def write_manifest(vault: Path, entries: list[dict[str, Any]]) -> None:
    """Atomically overwrite 00-meta/kb-manifest.json with entries."""
    path = vault / "00-meta" / "kb-manifest.json"
    atomic_write(path, json.dumps(entries, indent=2))


# ---------------------------------------------------------------------------
# Card helpers
# ---------------------------------------------------------------------------


def find_card(vault: Path, name: str) -> Path | None:
    """Scan 02-projects/**/<name>.md and return the first match, or None."""
    pattern = f"02-projects/**/{name}.md"
    matches = list(vault.glob(pattern))
    return matches[0] if matches else None


def find_cards(vault: Path, name: str) -> list[Path]:
    """Scan 02-projects/**/<name>.md and return ALL matches.

    Unlike find_card (which returns only the first hit), this returns every
    matching card — useful when a project has duplicate cards left behind by a
    botched move across group folders.
    """
    return list(vault.glob(f"02-projects/**/{name}.md"))


def delete_card(path: Path) -> None:
    """Delete a card file.  No-op if already absent."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def set_frontmatter_field(path: Path, field: str, value: str) -> None:
    """Rewrite a single top-level frontmatter scalar in a card file.

    Finds the first line inside the opening ``---`` block that begins with
    ``<field>:`` and replaces only that line.  All other lines (and the rest
    of the file body) are preserved verbatim.  Writes atomically.

    The value is written unquoted (``field: value``); callers are responsible
    for passing a plain YAML scalar that does not require quoting.
    """
    text = open(path, encoding="utf-8", newline="").read()
    lines = text.splitlines(keepends=True)

    # Locate the frontmatter block: content between the first and second "---".
    fm_start: int | None = None
    fm_end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            if fm_start is None:
                fm_start = i
            else:
                fm_end = i
                break

    if fm_start is None or fm_end is None:
        raise ValueError(f"No valid frontmatter block found in {path}")

    prefix = f"{field}:"
    replaced = False
    for i in range(fm_start + 1, fm_end):
        if lines[i].lstrip().startswith(prefix):
            # Preserve leading whitespace (should be none for top-level fields).
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            # Preserve the original line ending.
            ending = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = f"{indent}{field}: {value}{ending}"
            replaced = True
            break

    if not replaced:
        raise KeyError(f"Field {field!r} not found in frontmatter of {path}")

    atomic_write(path, "".join(lines))


def move_card(old_path: Path, new_path: Path) -> None:
    """Move a card file from old_path to new_path, creating parent dirs.

    Uses a read + atomic_write + delete sequence so the destination is always
    fully written before the source is removed (crash-safe on same or different
    volumes).
    """
    text = open(old_path, encoding="utf-8", newline="").read()
    atomic_write(new_path, text)
    old_path.unlink()


def group_of_path(path_rel: str) -> str:
    """Return the first path segment of a dev-root-relative path, or '' if none.

    Examples:
        group_of_path("1.1-dev-tools/projgamma") -> "1.1-dev-tools"
        group_of_path("projgamma")               -> ""
        group_of_path("")                      -> ""
    """
    parts = path_rel.split("/", 1)
    return parts[0] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# Edges helpers
# ---------------------------------------------------------------------------


def _compute_edges_remove(
    vault: Path, owner: str
) -> tuple[dict[str, Any], list[str]]:
    """Compute a new edges mapping with all references to *owner* removed.

    Returns (new_data, removed_descriptions) without writing to disk.
    removed_descriptions is a list of human-readable strings describing each
    removed reference (e.g. "my-project.requires: some-dependency").
    """
    edges_path = vault / "00-meta" / "project-edges.yaml"
    raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"project-edges.yaml: expected a mapping, got {type(data).__name__}"
        )

    removed: list[str] = []
    new_data: dict[str, Any] = {}

    for key, value in data.items():
        if key == owner:
            # Drop the entire top-level key belonging to the owner.
            removed.append(f"top-level key: {owner}")
            continue
        if isinstance(value, dict):
            new_inner: dict[str, Any] = {}
            for list_key, list_val in value.items():
                if isinstance(list_val, list):
                    filtered = [v for v in list_val if v != owner]
                    if len(filtered) < len(list_val):
                        for _ in range(len(list_val) - len(filtered)):
                            removed.append(f"{key}.{list_key}: {owner}")
                    new_inner[list_key] = filtered
                else:
                    new_inner[list_key] = list_val
            new_data[key] = new_inner
        else:
            new_data[key] = value

    return new_data, removed


def _leading_comment_block(raw: str) -> str:
    """Return the leading comment/blank-line block from a raw YAML string.

    Accumulates lines that start with ``#`` (possibly after whitespace) or are
    blank, stopping at the first line that begins a real YAML key.  Mirrors the
    same pattern used by the ledger module to preserve operator-authored headers
    on round-trips.
    """
    lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped in ("", "\n"):
            lines.append(line)
        else:
            break
    return "".join(lines)


def _dump_edges(data: dict[str, Any]) -> str:
    """Dump the edges mapping with FLOW-style lists (``requires: [a, b]``).

    kb-graph's project-edges.yaml reader parses list fields with an inline-list
    parser on the SAME line, so block-style lists (``requires:\\n  - a``) would be
    read as EMPTY and silently drop every edge. Force flow style for sequences
    while keeping the top-level mapping in block style (matching the original).
    """
    class _EdgeDumper(yaml.SafeDumper):
        pass

    def _flow_seq(dumper: yaml.SafeDumper, seq: Any) -> Any:
        return dumper.represent_sequence("tag:yaml.org,2002:seq", seq, flow_style=True)

    _EdgeDumper.add_representer(list, _flow_seq)
    return yaml.dump(
        data, Dumper=_EdgeDumper, default_flow_style=False, sort_keys=False, allow_unicode=True
    )


def rewrite_edges_remove(vault: Path, owner: str) -> list[str]:
    """Remove all references to *owner* from project-edges.yaml; return removed-ref descriptions.

    Writes the updated file atomically, preserving any leading comment/blank-line
    header block verbatim.  Returns an empty list when no refs were found (i.e.
    the file was not modified).
    """
    edges_path = vault / "00-meta" / "project-edges.yaml"
    raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    header = _leading_comment_block(raw)

    new_data, removed = _compute_edges_remove(vault, owner)
    if not removed:
        return []

    body = _dump_edges(new_data)
    atomic_write(edges_path, header + body)
    return removed


# ---------------------------------------------------------------------------
# Scout-cache helpers
# ---------------------------------------------------------------------------


def delete_scout_cache(vault: Path, name: str) -> bool:
    """Delete 00-meta/scout-cache/<name>.json if it exists.

    Returns True when the file was deleted, False when it was already absent
    (idempotent; mirrors the no-op-if-absent behaviour of delete_card).
    """
    path = vault / "00-meta" / "scout-cache" / f"{name}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Retired-projects helpers
# ---------------------------------------------------------------------------


def regen_retired_projects(vault: Path, owners: set[str]) -> None:
    """Regenerate 00-meta/retired-projects.txt from *owners*.

    The output is: a header comment line, then the sorted unique owners one
    per line.  Atomically replaces the existing file.
    """
    path = vault / "00-meta" / "retired-projects.txt"
    lines = ["# prune allowlist\n"] + [f"{o}\n" for o in sorted(owners)]
    atomic_write(path, "".join(lines))


def _load_retired_owners(vault: Path) -> set[str]:
    """Return the set of non-comment, non-blank owner names from retired-projects.txt."""
    path = vault / "00-meta" / "retired-projects.txt"
    if not path.exists():
        return set()
    owners: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            owners.add(stripped)
    return owners


# ---------------------------------------------------------------------------
# Rename primitives (Task 4)
# ---------------------------------------------------------------------------


def rename_scout_cache(vault: Path, old: str, new: str) -> bool:
    """Move scout-cache/<old>.json → scout-cache/<new>.json and update name fields.

    Updates BOTH the top-level ``name`` (the owner key kb-atomize uses to project
    cmd-/err-/adr- notes — leaving it stale resurrects <old> atomics on the next
    reproject) AND ``identity.name``.

    Returns True when the file was found and moved, False when already absent
    (idempotent — if <old>.json is gone there is nothing to do).
    """
    old_path = vault / "00-meta" / "scout-cache" / f"{old}.json"
    new_path = vault / "00-meta" / "scout-cache" / f"{new}.json"
    if not old_path.exists():
        return False
    data: dict[str, Any] = json.loads(old_path.read_text(encoding="utf-8"))
    # Top-level name = the owner key kb-atomize reads (`owner = scout["name"]`).
    if "name" in data:
        data["name"] = new
    # identity.name (harvest provenance) — keep consistent.
    if isinstance(data.get("identity"), dict):
        data["identity"]["name"] = new
    else:
        data["identity"] = {"name": new}
    atomic_write(new_path, json.dumps(data, ensure_ascii=False, indent=2))
    old_path.unlink()
    return True


def add_alias(card_path: Path, alias: str) -> bool:
    """Append *alias* to the ``aliases:`` flow-list in the frontmatter of *card_path*.

    Only-if-absent: if *alias* is already present the file is not modified and
    False is returned.  Returns True when the list was extended.

    Handles both ``aliases: []`` (empty) and ``aliases: [a, b]`` (non-empty).
    The value is appended to the existing flow-style list on the same line.
    """
    text = open(card_path, encoding="utf-8", newline="").read()
    lines = text.splitlines(keepends=True)

    # Locate frontmatter block.
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
        raise ValueError(f"No valid frontmatter block found in {card_path}")

    nl = "\r\n" if lines[fm_start].endswith("\r\n") else "\n"

    for i in range(fm_start + 1, fm_end):
        ln = lines[i]
        if not ln.lstrip().startswith("aliases:"):
            continue
        indent = ln[: len(ln) - len(ln.lstrip())]
        ending = "\r\n" if ln.endswith("\r\n") else "\n"
        m = re.search(r"\[([^\]]*)\]", ln)
        if m is not None:
            # Flow-style: ``aliases: [a, b]`` — append on the same line.
            inner = m.group(1).strip()
            existing: list[str] = []
            if inner:
                for part in inner.split(","):
                    cleaned = part.strip().strip('"').strip("'")
                    if cleaned:
                        existing.append(cleaned)
            if alias in existing:
                return False  # already present
            existing.append(alias)
            new_items = ", ".join(f'"{v}"' for v in existing)
            lines[i] = f"{indent}aliases: [{new_items}]{ending}"
            atomic_write(card_path, "".join(lines))
            return True
        # Block-style: ``aliases:`` followed by indented ``- item`` lines.
        item_indent = indent + "  "
        j = i + 1
        while j < fm_end and lines[j].lstrip().startswith("- "):
            if lines[j].lstrip()[2:].strip().strip('"').strip("'") == alias:
                return False  # already present
            j += 1
        lines.insert(j, f'{item_indent}- "{alias}"{ending}')
        atomic_write(card_path, "".join(lines))
        return True

    # No ``aliases:`` field at all — insert a flow-style line after the fence.
    lines.insert(fm_start + 1, f'aliases: ["{alias}"]{nl}')
    atomic_write(card_path, "".join(lines))
    return True


def rename_atomics(vault: Path, old: str, new: str) -> list[str]:
    """DELETE the *old* owner's atomic notes; kb-atomize regenerates them as *new*.

    The atomic layer (cmd-/err-/adr-/blk- notes + _INDEX) is FULLY GENERATED by
    kb-atomize from the scout-cache. Renaming the files in place is both redundant
    (kb-atomize overwrites them on the next reproject — often with a different
    canonical count) AND unsafe: kb-atomize-generated cross-links
    (``related: [[cmd-<old>-other]]``) inside the renamed notes are NOT the project
    wikilink, so they survive as DANGLING references to deleted notes.

    Generator-first fix: delete the old owner's atomics here (so no stale files or
    cross-links linger) and let kb-atomize regenerate the new owner's atomics from
    the renamed scout-cache (whose top-level ``name`` is now *new*) with correct
    cross-links and a regenerated _INDEX. The reconciler always prints the
    kb-atomize/kb-index reproject follow-up, so regeneration is part of the flow.

    Owner is taken from each file's ``tool:`` frontmatter scalar (ground truth),
    so a sibling owner sharing a hyphen-prefix (e.g. cmd-<old>-app-* owned by
    <old>-app) is never deleted. Returns the vault-relative paths deleted.

    *new* is unused by the deletion itself but kept in the signature for caller
    symmetry and to document intent.
    """
    _ = new  # regeneration under the new name is kb-atomize's responsibility
    deleted: list[str] = []
    patterns: list[tuple[Path, str]] = [
        (vault / "04-cli-errors", f"cmd-{old}-*.md"),
        (vault / "04-cli-errors", f"err-{old}-*.md"),
        (vault / "03-adr", f"{old}-adr-*.md"),
        (vault / "08-blockers", f"blk-{old}-*.md"),
    ]
    for folder, glob_pat in patterns:
        if not folder.exists():
            continue
        for src in sorted(folder.glob(glob_pat)):
            body = open(src, encoding="utf-8", newline="").read()
            tool_m = re.search(r"^tool:\s*[\"']?(.+?)[\"']?\s*$", body, re.MULTILINE)
            if tool_m is None or tool_m.group(1) != old:
                continue  # sibling owner — never touch
            try:
                src.unlink()
            except FileNotFoundError:
                continue
            deleted.append(str(src.relative_to(vault)))
    return deleted


def rewrite_wikilinks(vault: Path, old: str, new: str) -> int:
    """Rewrite ``[[<old>…]]`` wikilinks to ``[[<new>…]]`` across ALL ``*.md`` in the vault.

    Matches exactly on the node name using a lookahead for ``]``, ``|``, or ``#``
    so that a project name that is a prefix of another name is never mangled.

    Returns the count of files that were modified.
    """
    pattern = re.compile(r"\[\[" + re.escape(old) + r"(?=[\]|#])")
    replacement = f"[[{new}"
    changed = 0
    for md in vault.rglob("*.md"):
        text = open(md, encoding="utf-8", newline="").read()
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            atomic_write(md, new_text)
            changed += 1
    return changed


def rewrite_edges_rename(vault: Path, old: str, new: str) -> bool:
    """Rename all occurrences of *old* → *new* in project-edges.yaml.

    Handles:
    - Top-level key ``<old>:`` → ``<new>:``
    - List-item values ``<old>`` → ``<new>`` inside any mapping list.

    Preserves the leading comment/blank-line header verbatim (uses
    ``_leading_comment_block``).  Returns True when the file was modified,
    False when no references were found (i.e. already converged).
    """
    edges_path = vault / "00-meta" / "project-edges.yaml"
    raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"project-edges.yaml: expected a mapping, got {type(data).__name__}"
        )

    modified = False
    new_data: dict[str, Any] = {}

    for key, value in data.items():
        # Rename top-level key.
        new_key = new if key == old else key
        if new_key != key:
            modified = True
        if isinstance(value, dict):
            new_inner: dict[str, Any] = {}
            for list_key, list_val in value.items():
                if isinstance(list_val, list):
                    new_list = [new if v == old else v for v in list_val]
                    if new_list != list_val:
                        modified = True
                    new_inner[list_key] = new_list
                else:
                    new_inner[list_key] = list_val
            new_data[new_key] = new_inner
        else:
            new_data[new_key] = value

    if not modified:
        return False

    header = _leading_comment_block(raw)
    body = _dump_edges(new_data)
    atomic_write(edges_path, header + body)
    return True


# ---------------------------------------------------------------------------
# Absorb primitives (Task 5)
# ---------------------------------------------------------------------------


def _find_owned_atomics(vault: Path, owner: str) -> list[Path]:
    """Return atomic note paths whose ``tool:`` frontmatter scalar equals *owner*.

    Patterns scanned (mirrors rename_atomics):
      04-cli-errors/cmd-<owner>-*.md
      04-cli-errors/err-<owner>-*.md
      03-adr/<owner>-adr-*.md
      08-blockers/blk-<owner>-*.md

    The glob over-matches sibling owners (e.g. ``cmd-my-tool-extra-go.md`` when
    owner is ``my-tool``).  The ``tool:`` scalar is the authoritative filter.
    """
    patterns: list[tuple[Path, str]] = [
        (vault / "04-cli-errors", f"cmd-{owner}-*.md"),
        (vault / "04-cli-errors", f"err-{owner}-*.md"),
        (vault / "03-adr", f"{owner}-adr-*.md"),
        (vault / "08-blockers", f"blk-{owner}-*.md"),
    ]
    owned: list[Path] = []
    for folder, glob_pat in patterns:
        if not folder.exists():
            continue
        for src in sorted(folder.glob(glob_pat)):
            body = open(src, encoding="utf-8", newline="").read()
            tool_m = re.search(r"^tool:\s*[\"']?(.+?)[\"']?\s*$", body, re.MULTILINE)
            if tool_m is not None and tool_m.group(1) == owner:
                owned.append(src)
    return owned


def delete_atomics(vault: Path, owner: str) -> list[str]:
    """Delete atomic notes owned by *owner*; return vault-relative paths of deleted files.

    Uses ``_find_owned_atomics`` for the ``tool:`` ground-truth filter so sibling
    owners are never deleted.
    """
    deleted: list[str] = []
    for path in _find_owned_atomics(vault, owner):
        try:
            path.unlink()
        except FileNotFoundError:
            continue  # already gone (partial prior run) — stay idempotent
        deleted.append(str(path.relative_to(vault)))
    return deleted


def append_frontmatter_list_item(card_path: Path, field: str, value: str) -> bool:
    """Append *value* to the frontmatter list *field* in *card_path*.

    Only-if-absent: if *value* is already present the file is NOT modified and
    False is returned.  Returns True when the list was extended (or created).

    Handles three cases:
    - ``field: [a, b]``  — flow-style list on one line → append inline.
    - ``field:``         — block-style list (indented ``- item`` lines) → append entry.
    - field absent       — create ``field: ["value"]`` flow-style after the opening fence.

    Writes atomically.
    """
    text = open(card_path, encoding="utf-8", newline="").read()
    lines = text.splitlines(keepends=True)

    # Locate the frontmatter block (between first and second ``---``).
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
        raise ValueError(f"No valid frontmatter block found in {card_path}")

    nl = "\r\n" if lines[fm_start].endswith("\r\n") else "\n"

    for i in range(fm_start + 1, fm_end):
        ln = lines[i]
        if not ln.lstrip().startswith(f"{field}:"):
            continue
        indent = ln[: len(ln) - len(ln.lstrip())]
        ending = "\r\n" if ln.endswith("\r\n") else "\n"
        m = re.search(r"\[([^\]]*)\]", ln)
        if m is not None:
            # Flow-style: ``field: [a, b]`` — append inline.
            inner = m.group(1).strip()
            existing: list[str] = []
            if inner:
                for part in inner.split(","):
                    cleaned = part.strip().strip('"').strip("'")
                    if cleaned:
                        existing.append(cleaned)
            if value in existing:
                return False  # already present
            existing.append(value)
            new_items = ", ".join(f'"{v}"' for v in existing)
            lines[i] = f"{indent}{field}: [{new_items}]{ending}"
            atomic_write(card_path, "".join(lines))
            return True
        # Block-style: ``field:`` followed by indented ``- item`` lines.
        item_indent = indent + "  "
        j = i + 1
        while j < fm_end and lines[j].lstrip().startswith("- "):
            if lines[j].lstrip()[2:].strip().strip('"').strip("'") == value:
                return False  # already present
            j += 1
        lines.insert(j, f'{item_indent}- "{value}"{ending}')
        atomic_write(card_path, "".join(lines))
        return True

    # Field absent — insert a flow-style line after the opening fence.
    lines.insert(fm_start + 1, f'{field}: ["{value}"]{nl}')
    atomic_write(card_path, "".join(lines))
    return True


def rewrite_edges_repoint(vault: Path, src: str, dst: str) -> bool:
    """Replace all references to *src* with *dst* in project-edges.yaml, deduping.

    Handles:
    - Top-level key ``<src>:`` → ``<dst>:`` (merges into existing ``<dst>:`` if present).
    - List-item values ``<src>`` → ``<dst>`` inside any mapping list, deduping.

    Preserves the leading comment/blank-line header verbatim.  Returns True when
    the file was modified, False when no references to *src* were found.
    """
    edges_path = vault / "00-meta" / "project-edges.yaml"
    raw = edges_path.read_text(encoding="utf-8") if edges_path.exists() else ""
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"project-edges.yaml: expected a mapping, got {type(data).__name__}"
        )

    # Check whether src is referenced at all.
    src_referenced = src in data or any(
        src in list_val
        for v in data.values()
        if isinstance(v, dict)
        for list_val in v.values()
        if isinstance(list_val, list)
    )
    if not src_referenced:
        return False

    modified = False
    new_data: dict[str, Any] = {}

    for key, value in data.items():
        if key == src:
            # Merge src's inner mapping into dst's.  Defer writing to dst key.
            if not isinstance(value, dict):
                raise ValueError(
                    f"project-edges.yaml: key {src!r} must map to a mapping, "
                    f"got {type(value).__name__}"
                )
            modified = True
            if dst not in new_data:
                new_data[dst] = {}
            if isinstance(value, dict):
                dst_inner = new_data[dst]
                if not isinstance(dst_inner, dict):
                    dst_inner = {}
                    new_data[dst] = dst_inner
                for list_key, list_val in value.items():
                    if list_key in dst_inner and isinstance(dst_inner[list_key], list):
                        # Merge + dedup, preserving order.
                        existing_set = set(dst_inner[list_key])
                        for item in (list_val if isinstance(list_val, list) else [list_val]):
                            if item not in existing_set:
                                dst_inner[list_key].append(item)
                                existing_set.add(item)
                    else:
                        dst_inner[list_key] = list_val
            continue  # do not emit src key

        # Repoint list-item refs src → dst, deduping.
        if isinstance(value, dict):
            new_inner: dict[str, Any] = {}
            for list_key, list_val in value.items():
                if isinstance(list_val, list) and src in list_val:
                    modified = True
                    seen: set[str] = set()
                    new_list: list[Any] = []
                    for item in list_val:
                        effective = dst if item == src else item
                        if effective not in seen:
                            new_list.append(effective)
                            seen.add(effective)
                    new_inner[list_key] = new_list
                else:
                    new_inner[list_key] = list_val
            # If the src top-level key was already merged into new_data[dst] (because
            # src appeared before dst in the file), merge the dst key's own lists INTO
            # the already-built dict rather than overwriting it.
            if key == dst and key in new_data and isinstance(new_data[key], dict):
                dst_existing = new_data[key]
                for list_key, list_val in new_inner.items():
                    if list_key in dst_existing and isinstance(dst_existing[list_key], list):
                        existing_set = set(dst_existing[list_key])
                        for item in (list_val if isinstance(list_val, list) else [list_val]):
                            if item not in existing_set:
                                dst_existing[list_key].append(item)
                                existing_set.add(item)
                    else:
                        dst_existing[list_key] = list_val
            else:
                new_data[key] = new_inner
        else:
            new_data[key] = value

    # Ensure dst key exists in new_data if it was only created from a src top-level key.
    # (Already handled above via the merge path.)

    if not modified:
        return False

    header = _leading_comment_block(raw)
    body = _dump_edges(new_data)
    atomic_write(edges_path, header + body)
    return True


# ---------------------------------------------------------------------------
# Snapshot detection (Task 1 — kept here for cohesion)
# ---------------------------------------------------------------------------


def detect_snapshot(
    name: str,
    origin: str,
    name_by_remote: dict[str, str],
) -> bool:
    """Return True when origin matches a DIFFERENT project's remote.

    A directory whose git origin points to a remote that is already
    tracked under a different project name is a clone or snapshot — not
    an independent project.  It should be ignored by the reconciler and
    the kb-sync scanner.

    Args:
        name: The project name derived from the directory (e.g. stem of
              the directory path or card filename).
        origin: The git remote URL for 'origin' of that directory.
        name_by_remote: Mapping of remote URL → canonical project name,
                        built from the KB manifest or scout-cache.

    Returns:
        True  — origin is registered to a DIFFERENT project (snapshot).
        False — origin is unknown (new project) OR registered to *this*
                project (not a snapshot).
    """
    registered = name_by_remote.get(origin)
    # registered is None  → unknown remote, treat as real project
    # registered == name  → this is the canonical checkout, not a snapshot
    # registered != name  → different project owns this remote → snapshot
    return registered not in (None, name)
