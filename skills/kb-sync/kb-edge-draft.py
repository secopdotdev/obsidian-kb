#!/usr/bin/env python3
"""Review-gated edge-drafter for kb-sync (Spec: deterministic-harvest invariant).

STAGE → PAUSE → SUBMIT workflow for project-level `requires[]` / `goal` edges.
An LLM is never the source of truth — all values require human approval before
they are merged into any vault file.

Subcommands
-----------
prepare [--vault <v>] [--out <path>]
    Scan vault project cards (02-projects/**/*.md) and emit one worksheet block
    per project with empty `requires: []`, `goal: false`, and a `hints:` line
    of deterministic candidates.  Human edits the worksheet, fills in real deps.

    Default output: <vault>/00-meta/edge-draft-worksheet.md (atomic temp→replace).

apply <worksheet> [--vault <v>] [--apply]
    Parse a human-edited worksheet and propose merges into the durable sidecar.
    Default = DRY-RUN: print the sidecar diff only; nothing is written.
    --apply: merge idempotently into <vault>/00-meta/project-edges.yaml:
      - requires: union with existing value, dedupe, preserve order
      - goal: set only-if-currently-false/absent (never clobber existing true)
    If a project entry can't be resolved: FLAG to stderr, skip — never write.
    The sidecar is operator-owned and durable; it is NOT a generated file.

Design invariants
-----------------
- stdlib only; no LLM; no network; no PyYAML dependency.
- Atomic writes (temp → os.replace) with LF newlines.
- Idempotent: re-running --apply with the same worksheet is a no-op
  (sidecar content is byte-identical → _atomic_write skips the write).
- apply ignores the `hints:` line (informational only).
- apply never writes project card frontmatter — cards are GENERATED and would
  be silently overwritten on the next /kb-sync run.

Usage
-----
    py -3 kb-edge-draft.py --vault <vault> prepare
    # ... human edits worksheet ...
    py -3 kb-edge-draft.py --vault <vault> apply \\
        <vault>/00-meta/edge-draft-worksheet.md --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass  # pytest capture stubs may lack reconfigure


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WORKSHEET_NAME = "edge-draft-worksheet.md"
SIDECAR_NAME = "project-edges.yaml"

# Vault-relative path pattern for project cards.
_PROJECT_CARD_GLOB = "02-projects/**/*.md"

# Top-level vault dirs to exclude during card scan (mirrors kb-index.SKIP_DIRS).
_SKIP_DIRS: frozenset[str] = frozenset({"_templates", "active", ".obsidian"})


# ---------------------------------------------------------------------------
# Frontmatter helpers (house idiom — no PyYAML dependency)
# ---------------------------------------------------------------------------

def _fm_field(text: str, key: str) -> str | None:
    """Pull scalar `key: value` from a YAML frontmatter block; None if absent.

    Scans only the leading `---`-fenced block.  Strips surrounding quotes.
    Returns None for list fields (value starts with `[` or next line is `-`).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        body = lines
    else:
        body = []
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            body.append(ln)
    prefix = f"{key}:"
    for ln in body:
        if ln.startswith(prefix):
            raw = ln[len(prefix):].strip()
            # Don't try to parse list values — caller uses _parse_block_list_raw
            if raw.startswith("["):
                return ""  # signals "field present but is a list"
            return raw.strip('"').strip("'")
    return None


def _parse_block_list_raw(text: str, key: str) -> list[str]:
    """Parse a YAML block list field from frontmatter text.

    Handles both inline `key: [a, b]` and YAML block form:
        key:
          - a
          - b

    Returns a list of stripped string values (possibly empty).
    """
    lines = text.splitlines()
    # Find the frontmatter block.
    if lines and lines[0].strip() == "---":
        fm_lines: list[str] = []
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            fm_lines.append(ln)
    else:
        fm_lines = lines

    prefix = f"{key}:"
    result: list[str] = []
    in_block = False
    for i, ln in enumerate(fm_lines):
        if ln.startswith(prefix):
            raw = ln[len(prefix):].strip()
            if raw.startswith("["):
                # Inline list: [a, b, "c"]
                inner = raw.strip("[]")
                for item in inner.split(","):
                    item = item.strip().strip('"').strip("'")
                    if item:
                        result.append(item)
                in_block = False
            elif raw == "":
                # Block list follows — collect subsequent `  - …` lines.
                in_block = True
            else:
                # Bare scalar value on same line (not a list).
                in_block = False
            continue
        if in_block:
            m = re.match(r"^\s+-\s+(.*)", ln)
            if m:
                item = m.group(1).strip().strip('"').strip("'")
                if item:
                    result.append(item)
            elif ln.strip() and not ln.startswith(" ") and not ln.startswith("\t"):
                # Hit a non-indented line — end of block.
                in_block = False

    return result


def _extract_fm_text(text: str) -> str:
    """Return raw frontmatter block (between the two `---` fences)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    fm_lines: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            return "\n".join(fm_lines)
        fm_lines.append(ln)
    return ""  # unterminated


def _has_fm(text: str) -> bool:
    """Return True if the text has a valid `---` frontmatter fence."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for ln in lines[1:]:
        if ln.strip() == "---":
            return True
    return False


# ---------------------------------------------------------------------------
# Atomic write (temp → os.replace; LF newlines; skip if byte-identical)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> bool:
    """Write *text* to *path* atomically; return True if file changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------------------
# Project card reader
# ---------------------------------------------------------------------------

def read_project_cards(vault: Path) -> list[dict]:
    """Scan vault 02-projects/**/*.md and return a list of card dicts.

    Each dict has:
        path        vault-relative POSIX path
        abs_path    absolute Path object
        title       from frontmatter `title:` field
        group       from frontmatter `group:` field
        text        raw file text
    """
    cards: list[dict] = []
    projects_dir = vault / "02-projects"
    if not projects_dir.is_dir():
        return cards

    for md_path in sorted(projects_dir.glob("**/*.md")):
        try:
            rel = md_path.relative_to(vault)
        except ValueError:
            continue

        # Skip excluded dirs by top-level path parts.
        if set(rel.parts) & _SKIP_DIRS:
            continue

        # Skip _INDEX.md files (MOC, not project cards).
        if md_path.name.startswith("_"):
            continue

        try:
            text = md_path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            print(
                f"  WARNING: UTF-8 decode error in {md_path} — skipped",
                file=sys.stderr,
            )
            continue
        except OSError:
            continue

        if not _has_fm(text):
            continue

        note_type = _fm_field(text, "type")
        if note_type != "project":
            continue

        title = _fm_field(text, "title") or md_path.stem
        group = _fm_field(text, "group") or ""

        cards.append(
            {
                "path": rel.as_posix(),
                "abs_path": md_path,
                "title": title,
                "group": group,
                "text": text,
            }
        )

    return cards


# ---------------------------------------------------------------------------
# Graph.json reader (optional — for gate-derived hints)
# ---------------------------------------------------------------------------

def _read_graph_json(vault: Path) -> dict:
    """Load graph.json if present; return empty dict on failure."""
    graph_path = vault / "00-meta" / "graph.json"
    if not graph_path.exists():
        return {}
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_gate_hints(graph: dict) -> dict[str, list[str]]:
    """Return project-path → list[gate-id] mapping from graph.json edges.

    A gate-id is a hint for a project when the project has a `requires` edge
    leading to that gate (edge: project → gate, type="requires").
    Only gates that have other projects connected to them are surfaced (they're
    the gates shared across projects — highest signal for cross-project deps).
    """
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    gate_ids: set[str] = {n["id"] for n in nodes if n.get("type") == "gate"}

    # project_path → set of gate-ids it requires
    proj_to_gates: dict[str, set[str]] = {}
    for e in edges:
        s = e.get("s")
        d = e.get("d")
        if e.get("t") == "requires" and d in gate_ids and s is not None:
            proj_to_gates.setdefault(s, set()).add(d)

    # Return sorted lists for determinism.
    return {k: sorted(v) for k, v in sorted(proj_to_gates.items())}


# ---------------------------------------------------------------------------
# Worksheet renderer
# ---------------------------------------------------------------------------

_WORKSHEET_HEADER = """\
<!-- kb-edge-draft worksheet — STAGE → PAUSE → SUBMIT
     Fill in `requires` and `goal` for each project, then run:
         kb-edge-draft.py --vault <vault> apply <this-file> --apply
     Rules:
       - requires: list of project titles or gate-ids this project depends on.
       - goal: true if this project is an active goal-node (false = default).
       - hints: INFORMATIONAL ONLY — deterministic candidates; edit freely.
       - Leave `requires: []` and `goal: false` unchanged to skip a project.
     WARNING: values from this file are merged as-is.  Review before --apply.
     Changes are written to 00-meta/project-edges.yaml (durable sidecar),
     NOT directly to project card frontmatter.
-->

"""

_BLOCK_TEMPLATE = """\
## {title}

path: {path}
group: {group}
requires: []
goal: false
hints: {hints}

---

"""


def _build_hints(card: dict, all_cards: list[dict], gate_hints: dict[str, list[str]]) -> str:
    """Build a comma-separated hints string for one project card.

    Sources (deterministic, no LLM):
    1. Same-group sibling project titles (excluding self).
    2. Gate-ids from graph.json that this project already requires
       (or gates connected to same-group projects — shared gate hints).
    """
    hints: list[str] = []

    # 1. Same-group siblings.
    own_group = card.get("group", "")
    own_path = card.get("path", "")
    if own_group:
        for other in all_cards:
            if other["path"] != own_path and other.get("group") == own_group:
                hints.append(other["title"])

    # 2. Gate hints: gates this project already has (from existing requires),
    #    plus gates used by sibling projects (same group).
    existing_gates = set(gate_hints.get(own_path, []))
    for other in all_cards:
        if other.get("group") == own_group:
            existing_gates.update(gate_hints.get(other["path"], []))
    hints.extend(sorted(existing_gates))

    # Dedupe, preserve order (siblings first, then gates).
    seen: set[str] = set()
    deduped: list[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            deduped.append(h)

    return ", ".join(deduped) if deduped else "(none)"


def render_worksheet(cards: list[dict], gate_hints: dict[str, list[str]]) -> str:
    """Render the full worksheet markdown string."""
    parts: list[str] = [_WORKSHEET_HEADER]
    for card in cards:
        hints_str = _build_hints(card, cards, gate_hints)
        block = _BLOCK_TEMPLATE.format(
            title=card["title"],
            path=card["path"],
            group=card["group"],
            hints=hints_str,
        )
        parts.append(block)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Worksheet parser
# ---------------------------------------------------------------------------

def parse_worksheet(text: str) -> list[dict]:
    """Parse a human-edited worksheet into a list of project-entry dicts.

    Each dict has:
        title       str
        path        str (vault-relative POSIX)
        group       str
        requires    list[str]
        goal        bool
    Entries with requires==[] and goal==False are included (caller decides).
    The `hints:` line is silently ignored.

    `requires` accepts both:
      - Inline list form:  `requires: [a, b]`  or `requires: ["a", "b"]`
      - Bare comma-separated form: `requires: a, b`  (split on comma)
    """
    entries: list[dict] = []

    # Split on `## <title>` H2 headings (worksheet project sections).
    sections = re.split(r"\n## ", "\n" + text)
    for section in sections[1:]:  # first element is preamble/header
        lines = section.splitlines()
        if not lines:
            continue
        title = lines[0].strip()

        kv: dict[str, str] = {}
        for ln in lines[1:]:
            ln = ln.rstrip()
            if not ln or ln.strip() == "---" or ln.strip().startswith("<!--"):
                continue
            if ln.startswith("hints:"):
                continue  # informational; ignore
            if ":" in ln:
                key, _, val = ln.partition(":")
                key = key.strip()
                val = val.strip()
                if key in ("path", "group", "requires", "goal"):
                    kv[key] = val

        path = kv.get("path", "")
        group = kv.get("group", "")
        goal_raw = kv.get("goal", "false").lower()
        goal = goal_raw in ("true", "1", "yes")

        # Parse requires: inline list `[a, b]`, bare comma-sep `a, b`, or bare `a`.
        req_raw = kv.get("requires", "[]").strip()
        requires: list[str] = []
        if req_raw.startswith("["):
            # Inline list: [a, b, "c"]
            inner = req_raw.strip("[]")
            for item in inner.split(","):
                item = item.strip().strip('"').strip("'")
                if item:
                    requires.append(item)
        elif "," in req_raw:
            # Bare comma-separated: a, b, c
            for item in req_raw.split(","):
                item = item.strip().strip('"').strip("'")
                if item:
                    requires.append(item)
        elif req_raw:
            # Single bare value.
            requires.append(req_raw.strip('"').strip("'"))

        if not title or not path:
            continue  # malformed section

        entries.append(
            {
                "title": title,
                "path": path,
                "group": group,
                "requires": requires,
                "goal": goal,
            }
        )

    return entries


# ---------------------------------------------------------------------------
# Durable sidecar: 00-meta/project-edges.yaml
# ---------------------------------------------------------------------------
# Format (hand-emitted; stdlib-parseable):
#
#   # operator-authored project dependency edges (durable; merged by kb-graph)
#   <project-title>:
#     requires: ["title-a", "title-b"]
#     goal: true
#
# Rules:
#   - Top-level keys are project TITLES — a stable, directory-independent
#     identity.  kb-graph._resolve_link maps a title to its current node id
#     regardless of folder location, so edges survive filesystem reorgs.
#   - A project absent from the sidecar has no sidecar-derived edges.
#   - `goal: true` is explicit; goal:false/absent are equivalent.
#   - Keys are sorted for deterministic canonical form.
# ---------------------------------------------------------------------------

_SIDECAR_HEADER = (
    "# operator-authored project dependency edges"
    " (durable; merged by kb-graph)\n"
    "# Edit manually or via kb-edge-draft.py apply --apply\n"
    "# Key: project title (stable, directory-independent identity)\n"
)


def _parse_sidecar(text: str) -> dict[str, dict]:
    """Parse the project-edges.yaml sidecar into a dict.

    Returns:
        { project_path: {"requires": [...], "supersedes": [...], "partof": [...], "goal": bool} }

    Handles only the constrained format emitted by _emit_sidecar — no full YAML.
    """
    result: dict[str, dict] = {}
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Skip comments and blank lines at top level.
        if not line or line.lstrip().startswith("#"):
            if current_key is None:
                continue
            # Comments inside a project block are skipped.
            continue

        # Top-level key: does NOT start with whitespace.
        if not line[0].isspace() and line.endswith(":"):
            current_key = line[:-1].strip()
            if current_key not in result:
                result[current_key] = {"requires": [], "supersedes": [], "partof": [], "goal": False}
            continue

        # Indented field under a key.
        if current_key is not None:
            stripped = line.strip()
            if stripped.startswith("requires:"):
                req_raw = stripped[len("requires:"):].strip()
                if req_raw.startswith("[") and req_raw.endswith("]"):
                    inner = req_raw[1:-1]
                    items: list[str] = []
                    for item in inner.split(","):
                        item = item.strip().strip('"').strip("'")
                        if item:
                            items.append(item)
                    result[current_key]["requires"] = items
            elif stripped.startswith("supersedes:"):
                sup_raw = stripped[len("supersedes:"):].strip()
                if sup_raw.startswith("[") and sup_raw.endswith("]"):
                    inner = sup_raw[1:-1]
                    items2: list[str] = []
                    for item in inner.split(","):
                        item = item.strip().strip('"').strip("'")
                        if item:
                            items2.append(item)
                    result[current_key]["supersedes"] = items2
            elif stripped.startswith("partof:"):
                po_raw = stripped[len("partof:"):].strip()
                if po_raw.startswith("[") and po_raw.endswith("]"):
                    inner = po_raw[1:-1]
                    items3: list[str] = []
                    for item in inner.split(","):
                        item = item.strip().strip('"').strip("'")
                        if item:
                            items3.append(item)
                    result[current_key]["partof"] = items3
            elif stripped.startswith("goal:"):
                val = stripped[len("goal:"):].strip().lower()
                result[current_key]["goal"] = val in ("true", "1", "yes")

    return result


def _emit_sidecar(data: dict[str, dict]) -> str:
    """Emit the project-edges.yaml sidecar as a string.

    Canonical form:
    - Header comment block.
    - Sorted top-level keys (project paths).
    - For each key: `requires:` inline list (quoted items), then `goal: true`
      only if goal is True (omit false — absent == false).
    - Blank line between project blocks.
    - Trailing newline.
    """
    lines: list[str] = [_SIDECAR_HEADER]
    for key in sorted(data.keys()):
        entry = data[key]
        requires: list[str] = entry.get("requires", [])
        supersedes: list[str] = entry.get("supersedes", [])
        partof: list[str] = entry.get("partof", [])
        goal: bool = bool(entry.get("goal", False))

        lines.append(f"{key}:")
        req_inline = "[" + ", ".join(f'"{r}"' for r in requires) + "]"
        lines.append(f"  requires: {req_inline}")
        if supersedes:
            sup_inline = "[" + ", ".join(f'"{s}"' for s in supersedes) + "]"
            lines.append(f"  supersedes: {sup_inline}")
        if partof:
            po_inline = "[" + ", ".join(f'"{p}"' for p in partof) + "]"
            lines.append(f"  partof: {po_inline}")
        if goal:
            lines.append("  goal: true")
        lines.append("")  # blank line between entries

    return "\n".join(lines) + "\n"


def _load_sidecar(vault: Path) -> dict[str, dict]:
    """Load the project-edges sidecar; return empty dict if absent or unreadable."""
    sidecar_path = vault / "00-meta" / SIDECAR_NAME
    if not sidecar_path.exists():
        return {}
    try:
        text = sidecar_path.read_text(encoding="utf-8", errors="strict")
        return _parse_sidecar(text)
    except UnicodeDecodeError:
        print(
            f"  WARNING: UTF-8 decode error in {sidecar_path} — treating as empty",
            file=sys.stderr,
        )
        return {}
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _merge_requires(existing: list[str], proposed: list[str]) -> list[str]:
    """Union existing and proposed requires, deduped, preserving existing order."""
    seen: set[str] = set(existing)
    merged = list(existing)
    for item in proposed:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------

def cmd_prepare(vault: Path, out: Path) -> int:
    """Generate the review worksheet."""
    cards = read_project_cards(vault)
    if not cards:
        print("prepare: no project cards found in vault", file=sys.stderr)
        return 1

    graph = _read_graph_json(vault)
    gate_hints = _build_gate_hints(graph)

    worksheet = render_worksheet(cards, gate_hints)
    changed = _atomic_write(out, worksheet)
    verb = "wrote" if changed else "unchanged"
    print(
        f"prepare: {verb} {out.relative_to(vault).as_posix() if vault in out.parents else out}"
        f"  ({len(cards)} projects)",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: apply (sidecar-based, never touches project cards)
# ---------------------------------------------------------------------------

def cmd_apply(worksheet_path: Path, vault: Path, apply: bool) -> int:
    """Parse worksheet and merge into the durable sidecar (00-meta/project-edges.yaml).

    Never writes to project card frontmatter.  Project cards are GENERATED and
    changes written to them would be silently overwritten on the next /kb-sync run.
    """
    if not worksheet_path.exists():
        print(f"apply: worksheet not found: {worksheet_path}", file=sys.stderr)
        return 1

    try:
        ws_text = worksheet_path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        print(f"apply: UTF-8 decode error in worksheet: {exc}", file=sys.stderr)
        return 1

    entries = parse_worksheet(ws_text)

    if not entries:
        print("apply: no project entries found in worksheet", file=sys.stderr)
        return 1

    # Build lookups for validation. The sidecar is keyed by project TITLE (a
    # stable, directory-independent identity) — NOT by vault-relative path —
    # so edges survive folder reorganisations.  kb-graph._resolve_link resolves
    # a title key back to the current node regardless of where the file lives;
    # a stale full path would NOT resolve (res_map has no basename-with-.md key).
    cards = read_project_cards(vault)
    valid_paths: set[str] = {c["path"] for c in cards}
    valid_titles: set[str] = {c["title"] for c in cards}

    # Load the existing sidecar (base); we never drop projects not in this worksheet.
    sidecar = _load_sidecar(vault)

    dry_run = not apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"apply [{mode}]: {len(entries)} worksheet entries", file=sys.stderr)

    # Snapshot for diff output.
    original_sidecar = _emit_sidecar(sidecar) if sidecar else ""

    any_changes = False
    errors = 0

    for entry in entries:
        # Skip empty entries (no changes requested).
        if not entry["requires"] and not entry["goal"]:
            continue

        entry_path = entry["path"]
        entry_title = entry["title"]

        # Validate that the card exists (by path or title) — FLAG if not.
        if entry_path not in valid_paths and entry_title not in valid_titles:
            print(
                f"  FLAG: cannot resolve card for '{entry_title}' "
                f"(path={entry_path!r}) — skipped",
                file=sys.stderr,
            )
            errors += 1
            continue

        # Key the sidecar by TITLE (stable, directory-independent identity).
        key = entry_title

        # Merge into sidecar (in memory).
        existing_entry = sidecar.get(key, {"requires": [], "supersedes": [], "partof": [], "goal": False})
        existing_requires = existing_entry.get("requires", [])
        existing_supersedes = existing_entry.get("supersedes", [])
        existing_partof = existing_entry.get("partof", [])
        existing_goal = existing_entry.get("goal", False)

        merged_requires = _merge_requires(existing_requires, entry["requires"])
        # goal is monotonic: absent/false → true allowed; true never lowered.
        new_goal = existing_goal or (entry["goal"] and not existing_goal)

        requires_changed = merged_requires != existing_requires
        goal_changed = new_goal != existing_goal

        if not requires_changed and not goal_changed:
            continue  # no-op for this entry

        any_changes = True
        sidecar[key] = {
            "requires": merged_requires,
            "supersedes": existing_supersedes,  # preserve operator-authored lineage
            "partof": existing_partof,          # preserve operator-authored lineage
            "goal": new_goal,
        }

    new_sidecar_text = _emit_sidecar(sidecar)

    if not any_changes:
        print("apply: no changes (all entries already applied or empty)", file=sys.stderr)
        return 1 if errors else 0

    # Print diff (always — dry-run and apply alike show what changed).
    import difflib
    diff_lines = list(
        difflib.unified_diff(
            original_sidecar.splitlines(keepends=True),
            new_sidecar_text.splitlines(keepends=True),
            fromfile="project-edges.yaml (before)",
            tofile="project-edges.yaml (after)",
        )
    )
    if diff_lines:
        print("".join(diff_lines))

    if dry_run:
        print(
            "\napply: dry-run complete — re-run with --apply to write changes.",
            file=sys.stderr,
        )
        return 1 if errors else 0

    # Write sidecar atomically.
    sidecar_path = vault / "00-meta" / SIDECAR_NAME
    changed = _atomic_write(sidecar_path, new_sidecar_text)
    if changed:
        print(
            f"apply: wrote {sidecar_path.relative_to(vault).as_posix()}",
            file=sys.stderr,
        )
    else:
        print("apply: sidecar unchanged (byte-identical)", file=sys.stderr)

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Review-gated edge-drafter: propose and apply project dependency edges "
            "without letting an LLM guess become a harvested fact."
        )
    )
    ap.add_argument(
        "--vault",
        default=None,
        help="Root of the Obsidian vault (required; no default).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # --- prepare ---
    p_prep = sub.add_parser("prepare", help="Generate the human-review worksheet.")
    p_prep.add_argument(
        "--out",
        default=None,
        help=(
            "Output worksheet path "
            "(default: <vault>/00-meta/edge-draft-worksheet.md)"
        ),
    )

    # --- apply ---
    p_apply = sub.add_parser(
        "apply",
        help="Merge a reviewed worksheet into the durable sidecar.",
    )
    p_apply.add_argument("worksheet", help="Path to the human-edited worksheet.")
    p_apply.add_argument(
        "--apply",
        action="store_true",
        default=False,
        dest="do_apply",
        help="Write changes (default: dry-run only).",
    )

    args = ap.parse_args(argv)

    vault_str = args.vault or os.environ.get("KB_VAULT")
    if not vault_str:
        print("error: pass --vault <path> or set KB_VAULT", file=sys.stderr)
        return 1
    vault = Path(vault_str)
    if not vault.is_dir():
        print(f"error: vault directory not found: {vault}", file=sys.stderr)
        return 1

    if args.cmd == "prepare":
        out_path = Path(args.out) if args.out else vault / "00-meta" / DEFAULT_WORKSHEET_NAME
        return cmd_prepare(vault, out_path)

    if args.cmd == "apply":
        return cmd_apply(
            worksheet_path=Path(args.worksheet),
            vault=vault,
            apply=args.do_apply,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
