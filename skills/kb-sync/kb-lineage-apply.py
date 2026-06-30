#!/usr/bin/env python3
"""Atomic, only-if-blank writer for project-edges.yaml / objectives.yaml lineage.

Dependency-free (no pyyaml). Human values win over inference unless force=True.
The calling skill enforces STAGE->PAUSE->SUBMIT; this script performs the SUBMIT
write only.

Public API:
  apply_lineage(vault_root, project, *, advances, phase, milestones, requires,
                force=False) -> int          (0 ok, 2 on enum violation)
  apply_project_advances(vault_root, project, objectives) -> int
  main(argv=None) -> int                     (CLI front-end)

Known limitation: intra-block YAML comments in the *target* project's block are
lost on rewrite (the block is re-emitted from the parsed field set). Comments in
OTHER projects' blocks are preserved byte-identically because only the target
block is replaced.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# Fallback constants — used when lineage-enums.yaml is missing, unreadable, or empty.
# These are the SOURCE OF TRUTH for the fallback; the live source is 00-meta/lineage-enums.yaml.
_LANES_FALLBACK: frozenset[str] = frozenset(
    {"objective-a", "objective-b", "career", "home", "shared"}
)
_PHASES_FALLBACK: frozenset[str] = frozenset({"seed", "build", "harden", "ship"})

# Retained for argparse --help text (constructed at parse time before vault is known).
# DO NOT use these aliases for validation — always call _load_enums(vault_root) at runtime.
_LANES = _LANES_FALLBACK
_PHASES = _PHASES_FALLBACK

_SIDECAR_NAME = "project-edges.yaml"
_OBJECTIVES_NAME = "objectives.yaml"
_ENUMS_NAME = "lineage-enums.yaml"


# ---------------------------------------------------------------------------
# Data-driven enum loader
# ---------------------------------------------------------------------------

def _load_enums(vault_root: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Load lanes and phases from vault_root/00-meta/lineage-enums.yaml.

    Parser: dependency-free line-scan over the simple ``key:\\n  - value`` subset.
    Skips full-line ``#`` comments. Collects ``- value`` entries under the most
    recently seen top-level key (``lanes:`` or ``phases:``).

    Falls back gracefully per-key:
    - File missing / unreadable / empty → both fallback constants.
    - Key present but no items parsed → that key uses its fallback constant.
    """
    path = vault_root / "00-meta" / _ENUMS_NAME
    # Distinguish "no override file" (expected -> fallback) from a transient read error
    # (e.g. a Windows AV scan momentarily locking a just-written file). On the latter,
    # retry briefly before falling back, so a momentary lock cannot silently degrade
    # enum validation to the smaller fallback set (root cause of an intermittent flake).
    text: str | None = None
    for attempt in range(3):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return _LANES_FALLBACK, _PHASES_FALLBACK
        except ValueError:
            return _LANES_FALLBACK, _PHASES_FALLBACK
        except OSError:
            if attempt == 2:
                return _LANES_FALLBACK, _PHASES_FALLBACK
            import time
            time.sleep(0.05)
    if text is None:
        return _LANES_FALLBACK, _PHASES_FALLBACK

    parsed_lanes: list[str] = []
    parsed_phases: list[str] = []
    current_key: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        # Skip blank lines and full-line comments.
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level key (no leading whitespace, ends with ':').
        if line and not line[0].isspace() and line.endswith(":"):
            current_key = line[:-1].strip()
            continue
        # List item (leading whitespace, starts with '- ').
        if line[0].isspace() and stripped.startswith("- "):
            value = stripped[2:].split("#", 1)[0].strip().strip('"').strip("'")
            if not value:
                continue
            if current_key == "lanes":
                parsed_lanes.append(value)
            elif current_key == "phases":
                parsed_phases.append(value)

    lanes: frozenset[str] = frozenset(parsed_lanes) if parsed_lanes else _LANES_FALLBACK
    phases: frozenset[str] = frozenset(parsed_phases) if parsed_phases else _PHASES_FALLBACK
    return lanes, phases


# ---------------------------------------------------------------------------
# kb-graph bridge (importlib; same loader pattern used by kb-index / kb-harvest)
# ---------------------------------------------------------------------------

def _read_sidecar(vault_root: Path) -> dict:
    """Delegate to kb-graph._read_sidecar to stay in sync with the parse contract."""
    spec = importlib.util.spec_from_file_location(
        "kb_graph_la", Path(__file__).with_name("kb-graph.py"))
    if not (spec and spec.loader):
        raise RuntimeError("kb-graph.py not found next to kb-lineage-apply.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m._read_sidecar(vault_root)


# ---------------------------------------------------------------------------
# Milestone serialization
# ---------------------------------------------------------------------------

def _ser_milestones(milestones: list[dict]) -> str:
    """Serialize milestone dicts to inline-pipe list form for the sidecar.

    Each dict {title, phase, status} -> "title|phase|status" where missing
    phase emits an empty segment ("title||status").
    """
    items = []
    for ms in milestones or []:
        title = ms.get("title", "")
        phase = ms.get("phase") or ""          # None -> ""
        status = ms.get("status") or "todo"
        items.append(f'"{title}|{phase}|{status}"')
    return "[" + ", ".join(items) + "]"


def _ser_list(items: list[str]) -> str:
    """Serialize a list of strings to YAML inline list form: ["a", "b"]."""
    escaped = [f'"{item}"' for item in items]
    return "[" + ", ".join(escaped) + "]"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically: tempfile in same dir + os.replace.

    Ensures LF line endings and UTF-8 encoding. Creates parent directories if
    they do not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Guarantee single trailing newline.
    if not text.endswith("\n"):
        text += "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Block-level sidecar manipulation (project-edges.yaml)
# ---------------------------------------------------------------------------

def _is_top_level_key(line: str) -> bool:
    """Return True iff *line* is a top-level project key line.

    Contract mirrors _read_sidecar's classifier:
    - Non-empty after rstrip
    - First character is not whitespace (not indented)
    - Does not start with '#' after lstrip (not a comment)
    - Ends with ':'
    """
    r = line.rstrip()
    return bool(r) and not r[0].isspace() and not r.lstrip().startswith("#") and r.endswith(":")


def _build_block(project: str, fields: dict) -> list[str]:
    """Emit the YAML lines for one project block (key + 2-space-indented fields).

    Field emission order: requires, supersedes, partof, goal, advances, phase, milestones.
    Empty/None/False values are omitted (goal: false omitted; goal: true emitted).

    Returns a list of lines WITHOUT trailing newlines (caller adds them).
    """
    lines = [f"{project}:"]
    requires = fields.get("requires") or []
    if requires:
        lines.append(f"  requires: {_ser_list(requires)}")
    supersedes = fields.get("supersedes") or []
    if supersedes:
        lines.append(f"  supersedes: {_ser_list(supersedes)}")
    partof = fields.get("partof") or []
    if partof:
        lines.append(f"  partof: {_ser_list(partof)}")
    goal = fields.get("goal", False)
    if goal:
        lines.append("  goal: true")
    advances = fields.get("advances")
    if advances:
        lines.append(f"  advances: {advances}")
    phase = fields.get("phase")
    if phase:
        lines.append(f"  phase: {phase}")
    milestones = fields.get("milestones") or []
    if milestones:
        lines.append(f"  milestones: {_ser_milestones(milestones)}")
    return lines


def _merge_block(
    text: str,
    project: str,
    current: dict,
    advances,
    phase,
    milestones: list[dict],
    requires: list[str],
    supersedes: list[str],
    force: bool,
) -> str:
    """Rewrite ONLY the target project's block, leaving all other lines VERBATIM.

    Only-if-blank semantics (force=False):
      - scalar fields (advances, phase): keep current non-None value; only write
        new value when current is None OR force is True AND new is not None.
      - list fields (requires): keep current non-empty list; only write new
        value when current is empty OR force is True AND new is non-empty.
        An empty new arg NEVER wipes a non-empty current list (even under force).
      - milestones (list), supersedes (list): same empty-never-wipes rule as requires.
      - partof, goal: always preserved from current (not args to apply_lineage;
        they come through only via current).

    Preserves byte-identical lines for all projects other than the target.
    Appends a new block if the target project is absent.
    """
    # --- Resolve final field values (only-if-blank per field) ---
    def _pick_scalar(cur_val, new_val):
        """Return new_val if appropriate, else cur_val."""
        if force and new_val is not None:
            return new_val
        if cur_val is None and new_val is not None:
            return new_val
        return cur_val

    def _pick_list(cur_list, new_list):
        """Return new_list if appropriate, else cur_list. Empty new never wipes."""
        if not new_list:               # empty arg → never replace
            return cur_list
        if force:
            return new_list
        if not cur_list:               # blank current → accept new
            return new_list
        return cur_list                # human value wins

    final: dict = {
        "requires":    _pick_list(current.get("requires", []),   requires),
        "supersedes":  _pick_list(current.get("supersedes", []), supersedes),
        "partof":      current.get("partof", []),                # always preserve
        "goal":        current.get("goal", False),               # always preserve
        "advances":    _pick_scalar(current.get("advances"),     advances),
        "phase":       _pick_scalar(current.get("phase"),        phase),
        "milestones":  _pick_list(current.get("milestones", []), milestones),
    }

    new_block_lines = _build_block(project, final)

    # --- Split the existing text into: prefix lines, target block lines, suffix lines ---
    raw_lines = text.splitlines()  # no trailing \n per line

    prefix: list[str] = []
    target_block: list[str] = []
    suffix: list[str] = []

    state = "before"  # before | in_target | after
    for line in raw_lines:
        if state == "before":
            if _is_top_level_key(line) and line.rstrip()[:-1].strip() == project:
                state = "in_target"
                target_block.append(line)
            else:
                prefix.append(line)
        elif state == "in_target":
            if _is_top_level_key(line) and line.rstrip()[:-1].strip() != project:
                state = "after"
                suffix.append(line)
            else:
                target_block.append(line)
        else:  # after
            suffix.append(line)

    # Build the output: prefix + new block + suffix.
    # Insert a blank separator between sections when needed (mirror input style).
    out_lines: list[str] = []

    if prefix:
        out_lines.extend(prefix)
        # Add blank separator between prefix and new block if prefix doesn't end blank
        if prefix[-1].strip():
            out_lines.append("")

    out_lines.extend(new_block_lines)

    if suffix:
        # Add blank separator between new block and suffix
        out_lines.append("")
        out_lines.extend(suffix)

    # Handle append case (no existing block found — state still "before" → empty target_block).
    # NOTE: The out_lines built in the "found" path above (prefix + new_block + suffix) is
    # intentionally DISCARDED here and reconstructed from raw_lines.  This avoids a
    # spurious blank separator that the "found" path inserts between prefix and new block
    # (the prefix IS all lines when the project is absent, so no separator is wanted between
    # "everything" and the appended block — only a trailing blank before the new entry).
    # The two paths (found vs. append) are mutually exclusive: state=="before" iff the project
    # key was never encountered, meaning target_block is empty and prefix == raw_lines.
    if state == "before":
        out_lines = list(raw_lines)
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.extend(new_block_lines)

    result = "\n".join(out_lines)
    return result


# ---------------------------------------------------------------------------
# Public API: apply_lineage
# ---------------------------------------------------------------------------

def apply_lineage(
    vault_root,
    project: str,
    *,
    advances,
    phase,
    milestones: list[dict],
    requires: list[str],
    supersedes: list[str] | None = None,
    force: bool = False,
) -> int:
    """Update one project's entry in 00-meta/project-edges.yaml.

    Parameters
    ----------
    vault_root  : path to the vault root (str or Path)
    project     : project title (top-level YAML key)
    advances    : swim-lane enum value, or None to leave untouched
    phase       : maturity enum value, or None to leave untouched
    milestones  : list of {title, phase, status} dicts, or [] to leave untouched
    requires    : list of dependency project titles, or [] to leave untouched
    force       : if True, overwrite existing non-blank values

    Returns 0 on success, 2 on enum validation failure (no file written).

    Note: YAML comments inside the TARGET project's block are NOT preserved on
    rewrite (other projects are byte-identical).
    """
    vault_root = Path(vault_root)

    # --- Enum validation BEFORE any write (data-driven, with fallback) ---
    live_lanes, live_phases = _load_enums(vault_root)

    if advances is not None and advances not in live_lanes:
        print(
            f"kb-lineage-apply: invalid advances lane {advances!r}  "
            f"(allowed: {sorted(live_lanes)})",
            file=sys.stderr,
        )
        return 2
    if phase is not None and phase not in live_phases:
        print(
            f"kb-lineage-apply: invalid phase {phase!r}  "
            f"(allowed: {sorted(live_phases)})",
            file=sys.stderr,
        )
        return 2
    path = vault_root / "00-meta" / _SIDECAR_NAME

    # Read current values (only-if-blank source of truth).
    current = _read_sidecar(vault_root).get(project, {})

    # Read the raw file text (or empty string for a new file).
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    new_text = _merge_block(
        text, project, current, advances, phase, milestones, requires, supersedes or [], force
    )
    _atomic_write(path, new_text)
    return 0


# ---------------------------------------------------------------------------
# Public API: apply_project_advances
# ---------------------------------------------------------------------------

def apply_project_advances(vault_root, project: str, objectives: list[str]) -> int:
    """Set objectives.yaml project_advances[project] = objectives (unconditional overwrite
    of that one entry; the calling skill gates confirmation).

    Preserves the objectives section and all other project_advances entries verbatim.
    Appends both the project_advances section and the project entry if absent.
    Returns 0 on success.
    """
    vault_root = Path(vault_root)
    path = vault_root / "00-meta" / _OBJECTIVES_NAME

    text = path.read_text(encoding="utf-8") if path.exists() else ""

    new_text = _merge_project_advances(text, project, objectives)
    _atomic_write(path, new_text)
    return 0


def _merge_project_advances(text: str, project: str, objectives: list[str]) -> str:
    """Rewrite or insert `project_advances.<project>` in objectives.yaml.

    Only the target project's entry within project_advances is touched; all other
    lines are preserved VERBATIM (including the objectives section and other
    project_advances entries).

    The inline list is always written (even if empty), e.g.:
      projgamma: ["objective-a"]

    Strategy:
    1. Scan lines to find the project_advances: section.
    2. Within that section (indent==2 lines), find the target project entry.
    3. Replace that entry line (or insert at end of section / append section).
    """
    new_entry = f"  {project}: {_ser_list(objectives)}"
    lines = text.splitlines()
    out: list[str] = []

    in_pa_section = False
    project_found = False
    section_end_idx: int | None = None  # index where we'd insert if not found in-section

    i = 0
    while i < len(lines):
        line = lines[i]
        r = line.rstrip()

        # Detect top-level section header (indent 0, ends with ':')
        if r and not r[0].isspace() and not r.lstrip().startswith("#") and r.endswith(":"):
            key = r[:-1].strip()
            if key == "project_advances":
                in_pa_section = True
            else:
                # Entering a different top-level section.
                if in_pa_section and not project_found:
                    # Reached end of project_advances without finding project — insert before this section.
                    out.append(new_entry)
                    project_found = True
                in_pa_section = False
            out.append(line)
            i += 1
            continue

        if in_pa_section and r and not r[0].isspace():
            # Non-indented, non-section-header line inside project_advances context — shouldn't happen.
            out.append(line)
            i += 1
            continue

        if in_pa_section:
            # 2-space indented line (or blank) inside project_advances section.
            stripped_nc = r.strip()
            # Check if this is the target project entry.
            if stripped_nc and ":" in stripped_nc:
                # Split on first colon to get the key.
                colon_pos = stripped_nc.index(":")
                entry_key = stripped_nc[:colon_pos].strip()
                if entry_key == project:
                    # Replace this line.
                    out.append(new_entry)
                    project_found = True
                    i += 1
                    continue
            out.append(line)
            i += 1
            continue

        out.append(line)
        i += 1

    # If we reached EOF still in project_advances and never found the project, append.
    if in_pa_section and not project_found:
        out.append(new_entry)
        project_found = True

    # If project_advances section was never found at all, append it.
    if not project_found:
        if out and out[-1].strip():
            out.append("")
        out.append("project_advances:")
        out.append(new_entry)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Atomic lineage writer — SUBMIT step of STAGE→PAUSE→SUBMIT."
    )
    ap.add_argument("--vault", required=True, help="Path to vault root")
    ap.add_argument("--project", required=True, help="Project title (sidecar key)")
    ap.add_argument(
        "--advances",
        help=f"Swim-lane enum (allowed: {sorted(_LANES)})",
    )
    ap.add_argument(
        "--phase",
        help=f"Maturity enum (allowed: {sorted(_PHASES)})",
    )
    ap.add_argument(
        "--milestones",
        help='JSON list of {title, phase, status} dicts',
    )
    ap.add_argument(
        "--requires",
        help="Comma-separated dependency project titles",
    )
    ap.add_argument(
        "--supersedes",
        help="Comma-separated project titles this project supersedes",
    )
    ap.add_argument(
        "--objectives",
        help="Comma-separated objective slugs for project_advances update",
    )
    ap.add_argument("--force", action="store_true", help="Overwrite existing values")
    a = ap.parse_args(argv)

    import json

    milestones: list[dict] = json.loads(a.milestones) if a.milestones else []
    requires: list[str] = [s.strip() for s in a.requires.split(",")] if a.requires else []
    supersedes: list[str] = [s.strip() for s in a.supersedes.split(",")] if a.supersedes else []

    rc = apply_lineage(
        Path(a.vault),
        a.project,
        advances=a.advances,
        phase=a.phase,
        milestones=milestones,
        requires=requires,
        supersedes=supersedes,
        force=a.force,
    )
    if rc != 0:
        return rc

    if a.objectives:
        obj_list = [s.strip() for s in a.objectives.split(",")]
        rc = apply_project_advances(Path(a.vault), a.project, obj_list)

    return rc


if __name__ == "__main__":
    sys.exit(main())
