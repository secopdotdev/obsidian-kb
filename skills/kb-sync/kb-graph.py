#!/usr/bin/env python3
"""Deterministic graph-facts pass for the kb-sync knowledge base (Spec § graph-facts).

Reads parsed notes (path + raw text) emitted by kb-index.py, extracts directed
edges from frontmatter edge fields, detects cycles via Tarjan SCC (iterative),
computes topological rank (longest-path Kahn), and persists results to:
  - kb.sqlite  →  tables `edges` + `graph_facts`
  - 00-meta/graph.json  →  serialised graph for the web build (atomic write)

Invariants:
- Pure standard library only; no third-party dependencies.
- All iteration is sorted for determinism/idempotency.
- Node id == vault-relative POSIX path as keyed by kb-index (`notes.path`),
  EXCEPT inline gate markers whose node id == their declared gate-id string.
- Wikilinks are resolved path-first, then basename, then alias (see _resolve_link).
- Dangling edges (unresolvable endpoint) are dropped and counted in the return stats.
- Graph algorithms (SCC / topo_rank) use PREREQUISITE edges only:
  types `requires`, `blocked`, `partof`, `supersedes`, `gates`, `advances`.  The `related`
  type is associative / undirected and is excluded from cycle detection and topo
  ranking to prevent spurious giant cycles.  Related edges are still stored in
  the `edges` table and emitted in graph.json with cycle=0 / brk=False.
- Back-edge identification for topo_rank DAG: an iterative DFS over the
  prerequisite graph colours nodes grey (on recursion stack) / black (done).
  Edge (u→v) is a back-edge iff v is grey when the edge is traversed.  ALL
  back-edges are removed to form the residual DAG fed to longest-path Kahn,
  so every node receives a meaningful rank.
- Break-edge exposure:
    SQLite `break_edge=1`  — every DFS back-edge.
    JSON   `brk:true`      — ONE canonical back-edge per SCC (lex-min (src,dst)
                             among all back-edges touching that SCC); the others
                             get brk:false.  Callers use this for UI badge display.
- Adjacency lists are sorted before DFS/Tarjan so iteration order (and therefore
  back-edge selection) is deterministic across runs.

Gate recognition (DECLARED gates only — never inferred):
- Inline marker: <!-- @gate id=X status=open blocking=true gates=Y requires=[A,B] -->
  Detect ONLY the literal `<!-- @gate ` sigil (case-sensitive).
  Emits a synthetic node keyed by gate-id (no path).
- Artifact: a note with `type: gate` frontmatter.  Already a path-keyed node;
  `requires` edges extracted by normal frontmatter flow; `gates` extracted here.
- Gate edges are prerequisite-class (type "gates") and participate in cycle/topo.
  Direction: gate -> X "gates" (gate blocks X downstream);
             Y -> gate "requires" (Y must precede the gate).

Objectives sidecar (00-meta/objectives.yaml) — operator-authored, 2-level hierarchy:
  Schema:
    objectives:
      <slug>:
        label: "Human-readable label"
        kind: ultimate | milestone   # defaults to "milestone" when omitted
        advances: ["slug-a"]         # inline list; defaults to []
    project_advances:
      <project-title>: ["obj-slug"]  # inline list mapping project title → objective slugs

  Parsed by _read_objectives(); returns:
    {
      "objectives": {slug: {"label": str, "kind": str, "advances": [str]}},
      "project_advances": {title: [str]},
    }
  Missing file → {"objectives": {}, "project_advances": {}}. Unknown top-level sections ignored.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# kb-staleness lazy loader (hyphenated filename — must use importlib).
# Cached after first load; None means unavailable (tolerated gracefully).
#
# Import-loop safety — three structural invariants make circular loading
# impossible:
#   (a) Neither module's top-level code (module body) calls the other's
#       loader function; loading is deferred to the first call-site.
#   (b) module_from_spec / exec_module does NOT register the loaded module
#       in sys.modules under any name, so each side gets an independent
#       instance — there is no shared registry entry to trigger a re-entrant
#       import cycle.
#   (c) kb-staleness.compute() calls only parser helpers (_extract_fm_text,
#       _parse_scalar) that it loads from the kb-graph instance, never
#       build_graph_facts — so recursion cannot form even if both loaders
#       fire in the same call-stack.
# ---------------------------------------------------------------------------

_KB_STALENESS: object | None = None
_KB_STALENESS_LOADED: bool = False


def _load_staleness() -> object | None:
    """Load kb-staleness.py once and return its `compute` callable, or None."""
    global _KB_STALENESS, _KB_STALENESS_LOADED
    if _KB_STALENESS_LOADED:
        return _KB_STALENESS
    _KB_STALENESS_LOADED = True
    try:
        skill_dir = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "kb_staleness", skill_dir / "kb-staleness.py"
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _KB_STALENESS = getattr(mod, "compute", None)
    except Exception as exc:
        print(f"kb-staleness load failed: {exc}", file=sys.stderr)
        _KB_STALENESS = None
    return _KB_STALENESS


def _resolve_staleness_states(
    staleness: Mapping[str, str] | None,
    vault_root: Path,
) -> dict[str, str]:
    """Return a {project_name: state_string} map.

    Parameters
    ----------
    staleness:
        Pre-computed map (tests inject this for hermeticity).  When None,
        the real kb-staleness.compute() is called against *vault_root*.
    vault_root:
        Vault root passed through to compute() on the live path.

    The map produced by compute() has shape {name: {state, head, ...}};
    we reduce to {name: state} so callers only deal with a single string.

    Stem-collision caveat: the reduction keys by ``Path(card).stem``, so two
    cards with the same filename in different ``02-projects/<group>/``
    subdirectories would collide — the last one processed wins silently.
    """
    if staleness is not None:
        # Caller supplied a pre-computed map — use as-is.
        return dict(staleness)
    compute = _load_staleness()
    if compute is None:
        return {}
    try:
        records: dict[str, dict] = compute(vault_root)  # type: ignore[call-arg]
        return {name: (rec.get("state") or "unknown") for name, rec in records.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Sidecar reader — project-edges.yaml (written by kb-edge-draft.py apply)
# ---------------------------------------------------------------------------

_SIDECAR_NAME = "project-edges.yaml"
_OBJECTIVES_NAME = "objectives.yaml"


def _read_sidecar(vault_root: Path) -> dict[str, dict]:
    """Load 00-meta/project-edges.yaml; return {} if absent or unreadable.

    Format (constrained; hand-emitted by kb-edge-draft):
        <project-path>:
          requires: ["slug-a", "slug-b"]
          supersedes: ["old-slug"]
          partof: ["parent-slug"]
          goal: true          # only when true; absent == false
          # Lineage fields (operator-canonical; projected onto cards by the synth generator):
          advances: objective-a  # swim-lane enum (example): objective-a | objective-b |
                                 #   career | home | shared
          phase: build           # maturity column enum: seed | build | harden | ship
          milestones: ["MVP|build|done", "Beta|harden|todo"]
                                 # ordered pipe-delimited triples: title|phase|status
                                 # missing phase -> None; missing status -> "todo"

    Returns {
      project_key: {
        "requires": [...], "supersedes": [...], "partof": [...], "goal": bool,
        "advances": str|None, "phase": str|None, "milestones": list[dict],
      }
    }.

    Milestone dicts have the shape {"title": str, "phase": str|None, "status": str}.

    NOTE: this parser is LIBERAL — it does NOT enforce the enum values for
    `advances` or `phase`; that validation lives in the writer (kb-edge-draft).
    Unknown keys are silently ignored (tolerant line-parser by design).
    """
    sidecar_path = vault_root / "00-meta" / _SIDECAR_NAME
    if not sidecar_path.exists():
        return {}
    try:
        text = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, dict] = {}
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if not line or line.lstrip().startswith("#"):
            continue

        # Top-level key: no leading whitespace, ends with ':'
        if not line[0].isspace() and line.endswith(":"):
            current_key = line[:-1].strip()
            if current_key not in result:
                result[current_key] = {
                    "requires": [], "supersedes": [], "partof": [], "goal": False,
                    "advances": None, "phase": None, "milestones": [],
                }
            continue

        if current_key is not None:
            stripped = line.strip()
            if stripped.startswith("requires:"):
                result[current_key]["requires"] = _parse_inline_list(
                    stripped[len("requires:"):].strip()
                )
            elif stripped.startswith("supersedes:"):
                result[current_key]["supersedes"] = _parse_inline_list(
                    stripped[len("supersedes:"):].strip()
                )
            elif stripped.startswith("partof:"):
                result[current_key]["partof"] = _parse_inline_list(
                    stripped[len("partof:"):].strip()
                )
            elif stripped.startswith("goal:"):
                val = stripped[len("goal:"):].strip().lower()
                result[current_key]["goal"] = val in ("true", "1", "yes")
            elif stripped.startswith("advances:"):
                result[current_key]["advances"] = _sidecar_scalar(stripped[len("advances:"):])
            elif stripped.startswith("phase:"):
                result[current_key]["phase"] = _sidecar_scalar(stripped[len("phase:"):])
            elif stripped.startswith("milestones:"):
                result[current_key]["milestones"] = _parse_milestones(
                    stripped[len("milestones:"):].strip()
                )

    return result


def _strip_trailing_comment(value: str) -> str:
    """Strip a trailing YAML inline comment from an unquoted scalar or bare token.

    A trailing comment begins with space-hash (` #`).  Only the first such
    occurrence is removed; everything before it is returned, right-stripped.
    This must NOT be called on a value that is a quoted string — use
    _extract_quoted_label for those.
    """
    idx = value.find(" #")
    if idx != -1:
        value = value[:idx]
    return value.rstrip()


def _extract_quoted_label(raw_value: str) -> str:
    """Extract the content of a quoted YAML scalar, ignoring anything after the closing quote.

    Handles both single and double quotes.  If the value is not quoted, the raw
    value is returned as-is (no stripping — callers should strip beforehand).
    A hash inside the quotes (e.g. `"C # guide"`) is preserved exactly.
    """
    raw_value = raw_value.strip()
    if not raw_value:
        return raw_value
    q = raw_value[0]
    if q not in ('"', "'"):
        # Not a quoted scalar; return as-is (caller may apply _strip_trailing_comment).
        return raw_value
    close = raw_value.find(q, 1)
    if close == -1:
        # Unterminated quote — return everything after the opening quote defensively.
        return raw_value[1:]
    return raw_value[1:close]


def _strip_after_closing_bracket(raw: str) -> str:
    """Strip anything after the last `]` in an inline-list value string.

    Handles a trailing comment such as `["slug-a"]  # note` — returns `["slug-a"]`.
    If no `]` is present the raw string is returned unchanged.
    """
    idx = raw.rfind("]")
    if idx != -1:
        return raw[: idx + 1]
    return raw


def _read_objectives(vault_root: Path) -> dict[str, dict]:
    """Load 00-meta/objectives.yaml; return empty structure if absent or unreadable.

    Format (operator-authored; 2-level indent hierarchy):
        objectives:
          <slug>:
            label: "Human-readable label"
            kind: ultimate          # bare scalar; defaults to "milestone" if omitted
            advances: ["slug-a"]    # inline list; defaults to []
                                    # advances / project_advances MUST use inline list
                                    # form (["a","b"]); block-list (`- a` on following
                                    # lines) and bare scalars are NOT supported and will
                                    # parse as empty.
        project_advances:
          <project-title>: ["obj-slug"]

    Returns:
        {
            "objectives":      {slug: {"label": str, "kind": str, "advances": [str]}},
            "project_advances": {title: [str]},
        }

    Unknown top-level sections are silently ignored.
    """
    objectives_path = vault_root / "00-meta" / _OBJECTIVES_NAME
    empty: dict[str, dict] = {"objectives": {}, "project_advances": {}}

    if not objectives_path.exists():
        return empty
    try:
        text = objectives_path.read_text(encoding="utf-8")
    except OSError:
        return empty

    objectives: dict[str, dict] = {}
    project_advances: dict[str, list] = {}

    # Parse state
    current_section: str | None = None  # "objectives" | "project_advances" | ignored
    current_slug: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Skip blank lines and comment lines.
        if not line or line.lstrip().startswith("#"):
            continue

        # Compute indent (number of leading spaces).
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0:
            # Top-level section header — must end with ':' and have no value after it.
            # Strip a trailing inline comment (e.g. `objectives:  # main`) before matching.
            stripped_no_comment = _strip_trailing_comment(stripped)
            if stripped_no_comment.endswith(":"):
                current_section = stripped_no_comment[:-1]
                current_slug = None
            # Non-colon top-level lines are ignored (shouldn't appear in this schema).
            continue

        if indent == 2:
            # 2-space indent: slug key under "objectives" or title key under "project_advances".
            # Strip trailing comments from the whole stripped line before classification.
            stripped_nc = _strip_trailing_comment(stripped)
            if not stripped_nc.endswith(":") and ":" not in stripped_nc:
                # Malformed — ignore.
                current_slug = None
                continue

            if current_section == "objectives":
                # Key is a slug; value (if any) is ignored — fields are at indent 4.
                if stripped_nc.endswith(":"):
                    current_slug = stripped_nc[:-1].strip()
                else:
                    # slug: some-value  (unexpected but tolerate — use key part)
                    current_slug = stripped_nc.split(":", 1)[0].strip()
                # Initialise with defaults.
                if current_slug not in objectives:
                    objectives[current_slug] = {
                        "label": "",
                        "kind": "milestone",
                        "advances": [],
                    }

            elif current_section == "project_advances":
                # Format: <title>: ["slug", ...]  — value is always on the same line.
                colon_pos = stripped_nc.index(":")
                title = stripped_nc[:colon_pos].strip()
                raw_value = stripped_nc[colon_pos + 1:].strip()
                # Strip any trailing comment after the closing ].
                raw_value = _strip_after_closing_bracket(raw_value)
                project_advances[title] = _parse_inline_list(raw_value)
                current_slug = None  # project_advances has no sub-fields

            else:
                # Unknown section — ignore its lines.
                current_slug = None

            continue

        if indent == 4 and current_section == "objectives" and current_slug is not None:
            # 4-space indent: field under a slug in the objectives section.
            if ":" not in stripped:
                continue  # malformed field line; skip

            colon_pos = stripped.index(":")
            field_key = stripped[:colon_pos].strip()
            raw_value = stripped[colon_pos + 1:].strip()

            if field_key == "label":
                # Quoted scalar — extract content between the quotes; preserves
                # hashes inside quotes (e.g. "C# guide") and ignores trailing comments.
                objectives[current_slug]["label"] = _extract_quoted_label(raw_value)
            elif field_key == "kind":
                # Bare scalar — strip trailing inline comment, then dequote defensively.
                objectives[current_slug]["kind"] = (
                    _strip_trailing_comment(raw_value).strip('"').strip("'")
                )
            elif field_key == "advances":
                # Inline list — strip any trailing comment after the closing ] first.
                objectives[current_slug]["advances"] = _parse_inline_list(
                    _strip_after_closing_bracket(raw_value)
                )
            # Unknown field keys are silently ignored.
            continue

        # Lines at indent > 4 or other unexpected indents are ignored.

    return {"objectives": objectives, "project_advances": project_advances}


sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# DDL — CREATE IF NOT EXISTS so this is safe to call on an existing db.
# We DELETE + re-insert each run for idempotency.
# ---------------------------------------------------------------------------
_DDL_EDGES = """\
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    type       TEXT NOT NULL,
    cycle      INTEGER NOT NULL DEFAULT 0,
    break_edge INTEGER NOT NULL DEFAULT 0
)
"""

_DDL_GRAPH_FACTS = """\
CREATE TABLE IF NOT EXISTS graph_facts (
    slug       TEXT PRIMARY KEY,
    cycle_id   INTEGER,
    topo_rank  INTEGER
)
"""

# Edge types that carry prerequisite / dependency semantics and participate in
# cycle detection and topo ranking.
# "gates" is included: a gate-node sequencing work is as strong a prerequisite
# relationship as "requires" or "supersedes".
# "advances" is included: an objective/project advancing another is a directional
# dependency (advancer is upstream; the advanced objective is downstream).
_PREREQ_TYPES: frozenset[str] = frozenset({"requires", "blocked", "partof", "supersedes", "gates", "advances"})


# ---------------------------------------------------------------------------
# Frontmatter list/edge parsing (does NOT reuse parse_frontmatter which skips
# list fields).  We parse directly from the raw text for edge fields only.
# ---------------------------------------------------------------------------

def _extract_fm_text(text: str) -> str:
    """Return the raw frontmatter block (between the --- fences) or ''."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    fm_lines: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            return "\n".join(fm_lines)
        fm_lines.append(ln)
    return ""  # unterminated


def _parse_inline_list(raw: str) -> list[str]:
    """Parse YAML inline list string: `["a", "b"]` or `[a, b]` → list of strings."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    inner = raw[1:-1]
    if not inner.strip():
        return []
    items: list[str] = []
    for item in inner.split(","):
        item = item.strip().strip('"').strip("'")
        if item:
            items.append(item)
    return items


def _sidecar_scalar(raw: str) -> str | None:
    """Clean a sidecar scalar value to a string (or None if empty).

    Routes on quotedness FIRST: a quoted value goes through _extract_quoted_label
    (which preserves a ' #' inside the quotes); an unquoted value goes through
    _strip_trailing_comment (which is unsafe on quoted values — see its docstring).
    """
    s = raw.strip()
    if s and s[0] in ('"', "'"):
        val = _extract_quoted_label(s)
    else:
        val = _strip_trailing_comment(s)
    return val.strip() or None


def _parse_milestones(raw: str) -> list[dict]:
    """Parse `["title|phase|status", ...]` into ordered milestone dicts.

    Inline-list form only (dependency-free, same parser family as edges).
    Empty/missing phase -> None; missing status -> 'todo'. Blank title skipped.
    """
    out: list[dict] = []
    for item in _parse_inline_list(_strip_after_closing_bracket(raw)):
        parts = [p.strip() for p in item.split("|")]
        title = parts[0] if parts else ""
        if not title:
            continue
        out.append({
            "title": title,
            "phase": parts[1] if len(parts) > 1 and parts[1] else None,
            "status": parts[2] if len(parts) > 2 and parts[2] else "todo",
        })
    return out


def _parse_block_list(fm_text: str, key: str) -> list[str]:
    """Parse a YAML block-list field, returning only string-type items.

    Handles:
    - Inline list:  `key: ["[[a]]", "[[b]]"]`
    - Block list of scalars:
        key:
          - "[[a]]"
          - "[[b]]"
    - Block list of dicts (e.g. `blockers` with text/severity):
        key:
          - text: "..."  ← these are NOT slug refs; returned empty so dicts
                           never produce dangling counts (they aren't refs).
    Returns a list of string values for the key.
    """
    # First try inline on same line. Use [ \t]* (NOT \s*) so the post-colon
    # whitespace cannot swallow the newline+indent of a block list and pull the
    # first "- item" onto same_line (which would mis-classify the block as a
    # non-list scalar and return []). Block lists must fall through to the scan.
    pattern = re.compile(r"^" + re.escape(key) + r":[ \t]*(.*)", re.MULTILINE)
    m = pattern.search(fm_text)
    if not m:
        return []
    same_line = m.group(1).strip()

    if same_line.startswith("["):
        return _parse_inline_list(same_line)

    # Empty scalar or value that's not a list → not a list field.
    if same_line and not same_line == "":
        return []

    # Block list: scan lines after the key line.
    key_line_pos = m.start()
    tail = fm_text[m.end():]
    items: list[str] = []
    for ln in tail.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if not stripped.startswith("-"):
            # Non-list line = end of block for this key (new key or section).
            break
        val = stripped[1:].strip()
        # Skip dict items (contain ': ' after non-wikilink content).
        # Dict items look like: `text: "..."`, `severity: high`
        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_-]*\s*:', val):
            # This is a dict key-value — skip the entire dict block.
            continue
        # Strip surrounding quotes.
        val = val.strip('"').strip("'")
        if val:
            items.append(val)
    return items


def _parse_scalar(fm_text: str, key: str) -> str:
    """Return the scalar value for *key*, or '' if absent/empty."""
    pattern = re.compile(r"^" + re.escape(key) + r":\s*(.*)", re.MULTILINE)
    m = pattern.search(fm_text)
    if not m:
        return ""
    val = m.group(1).strip().strip('"').strip("'")
    return val


def _strip_wikilink(text: str) -> str:
    """Extract the target from a wikilink like [[target|display]] or [[target#heading]].

    Returns the target part only (no display label, no heading).
    """
    text = text.strip()
    # Remove surrounding [[ ]]
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    # Drop display label (`|`)
    if "|" in text:
        text = text.split("|", 1)[0]
    # Drop heading anchor
    if "#" in text:
        text = text.split("#", 1)[0]
    return text.strip()


def _split_wikilinks(raw: str) -> list[str]:
    """Split a scalar containing one or more comma-separated wikilinks.

    Handles:  `[[a]], [[b]], [[c]]`  →  ['a', 'b', 'c']
    Also handles bare strings (no brackets) returned as a single item if non-empty.
    """
    if "[[" in raw:
        # Extract all [[...]] occurrences.
        targets = re.findall(r"\[\[([^\]]+)\]\]", raw)
        results: list[str] = []
        for t in targets:
            # Strip display labels and headings.
            if "|" in t:
                t = t.split("|", 1)[0]
            if "#" in t:
                t = t.split("#", 1)[0]
            t = t.strip()
            if t:
                results.append(t)
        return results
    # Plain scalar (no brackets) — return as single item if non-empty.
    val = raw.strip()
    if val and val not in ('""', "''"):
        return [val]
    return []


def _count_fm_list_items(text: str, key: str) -> int:
    """Count items in a YAML block-list field by direct line scanning.

    Works by finding the `key:` line in the frontmatter, then counting
    subsequent `  - ` lines until a non-list line or fence end is reached.
    Also handles inline lists: `key: [a, b, c]` → count of comma-separated items.

    Uses a line-scan rather than _parse_block_list to avoid the `\\s*` ambiguity
    where the key-matching pattern absorbs the newline after `key:` and merges
    the first list item into the scalar value.
    """
    fm_text = _extract_fm_text(text)
    if not fm_text:
        return 0

    lines = fm_text.splitlines()
    key_prefix = key + ":"
    in_list = False
    count = 0

    for line in lines:
        stripped = line.strip()
        if in_list:
            if stripped.startswith("-"):
                count += 1
            else:
                break  # end of block list
        elif stripped == key_prefix or stripped.startswith(key_prefix + " "):
            # Same-line value?
            same_line = stripped[len(key_prefix):].strip()
            if same_line.startswith("[") and same_line.endswith("]"):
                # Inline list: count comma-separated items.
                inner = same_line[1:-1].strip()
                if inner:
                    count = len([s for s in inner.split(",") if s.strip()])
                break
            elif same_line:
                # Scalar value — not a list.
                break
            else:
                # Empty same-line value → block list follows.
                in_list = True

    return count


def _extract_aliases_from_text(text: str) -> list[str]:
    """Parse the aliases: field from raw note text, handling both inline forms:
    `["quoted alias", "another"]` and `[bare, alias]`.
    """
    fm_text = _extract_fm_text(text)
    # Try inline list on the aliases: line.
    m = re.search(r"^aliases:\s*(.*)", fm_text, re.MULTILINE)
    if not m:
        return []
    same_line = m.group(1).strip()
    if same_line.startswith("["):
        return _parse_inline_list(same_line)
    return []


# ---------------------------------------------------------------------------
# Gate parsing — DECLARED gates only (inline marker + type:gate artifact)
# ---------------------------------------------------------------------------

# Inline gate sigil — MUST match this exact literal prefix (case-sensitive).
# Bare "gate" prose must never trigger detection.
_GATE_SIGIL = "<!-- @gate "

# Attribute tokenizer: key=value where value is either [bracketed,list] or
# a single space-free token.  This is the canonical grammar implemented here:
#   attr_list  = attr_pair*
#   attr_pair  = KEY '=' VALUE
#   KEY        = [\w-]+
#   VALUE      = '[' [^\]]* ']'     (bracket list: "requires=[a,b]")
#              | [^\s>]+             (bare token:   "gates=x,y" "blocking=true")
# Note: space-separated lists inside brackets are also handled (split on , or space).
_GATE_ATTR_RE = re.compile(r"([\w-]+)=(\[[^\]]*\]|[^\s>]+)")


def _parse_gate_list(raw: str) -> list[str]:
    """Parse a gate list value: '[a, b]', '[a b]', 'a,b', or 'a' → list of slugs."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
    else:
        inner = raw
    # Split on commas or whitespace, filter empty.
    items = [s.strip() for s in re.split(r"[,\s]+", inner) if s.strip()]
    return items


def _parse_gate_bool(raw: str) -> bool:
    """Parse a boolean attribute value from a gate marker."""
    return raw.strip().lower() in ("true", "1", "yes")


def _parse_inline_gates(text: str) -> tuple[list[dict], int]:
    """Scan *text* for inline gate markers and return (gates, skipped_count).

    Each gate dict has keys:
      id (str), status (str), blocking (bool), gates (list[str]),
      requires (list[str]), ref (str|None), criteria (list[str]).

    Markers with a missing or empty 'id' are NOT included in the list;
    they increment the skipped_count instead.

    Markers inside fenced code blocks (``` or ~~~) are ignored — they are
    documentation examples, not DECLARED gates.  Real gate declarations are
    unfenced HTML comments (invisible in rendered Markdown).
    """
    gates: list[dict] = []
    skipped = 0
    lines = text.splitlines()
    in_fence = False
    _FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

    i = 0
    while i < len(lines):
        line = lines[i]

        # Track fenced code block state.  A fence opens or closes on a line
        # whose stripped form starts with ``` or ~~~.
        stripped_line = line.strip()
        if _FENCE_RE.match(stripped_line):
            in_fence = not in_fence
            i += 1
            continue

        # Skip sigil detection while inside a fenced block.
        if in_fence:
            i += 1
            continue

        # Fast pre-check for the sigil before running regex.
        if _GATE_SIGIL not in line:
            i += 1
            continue

        # Extract the full comment text between <!-- and -->.
        m = re.search(r"<!--\s*@gate\b(.*?)-->", line)
        if not m:
            i += 1
            continue

        attr_body = m.group(1)
        attrs: dict[str, str] = {
            k: v for k, v in _GATE_ATTR_RE.findall(attr_body)
        }

        gate_id = attrs.get("id", "").strip()
        if not gate_id:
            skipped += 1
            i += 1
            continue

        # Collect criteria: - [ ] / - [x] lines immediately following (until
        # blank line or heading).
        criteria: list[str] = []
        j = i + 1
        while j < len(lines):
            crit_line = lines[j]
            stripped = crit_line.strip()
            if not stripped:
                break  # blank line terminates criteria block
            if stripped.startswith("#"):
                break  # heading terminates criteria block
            # Match Markdown task-list items (checked or unchecked).
            cm = re.match(r"^[-*]\s+\[[ xX]\]\s+(.*)", stripped)
            if cm:
                criteria.append(cm.group(1).strip())
            j += 1

        gates.append({
            "id": gate_id,
            "status": attrs.get("status", "open"),
            "blocking": _parse_gate_bool(attrs.get("blocking", "false")),
            "gates": _parse_gate_list(attrs.get("gates", "")),
            "requires": _parse_gate_list(attrs.get("requires", "")),
            "ref": attrs.get("ref"),
            "criteria": criteria,
        })
        i += 1

    return gates, skipped


def _collect_gates(
    notes: list[dict],
) -> tuple[dict[str, dict], int, int]:
    """Scan all notes for DECLARED gate markers and artifact gate notes.

    Returns:
      inline_gates  — dict gate_id → gate dict (inline markers only;
                      artifact gates stay path-keyed and are handled separately)
      total_gates   — count of successfully parsed inline gate nodes
      skipped_gates — count of malformed inline markers (missing id)
    """
    inline_gates: dict[str, dict] = {}
    total_gates = 0
    total_skipped = 0

    for note in sorted(notes, key=lambda n: n["path"]):
        raw_text = note.get("text", "")
        if _GATE_SIGIL not in raw_text:
            continue  # Fast skip — no sigil in this note at all.

        gates, skipped = _parse_inline_gates(raw_text)
        total_skipped += skipped
        for g in gates:
            gid = g["id"]
            if gid not in inline_gates:
                inline_gates[gid] = g
                total_gates += 1
            # Duplicate gate ids are silently deduped (first occurrence wins,
            # consistent with res_map fill-if-absent policy).

    return inline_gates, total_gates, total_skipped


def _extract_gate_edges(
    gate_id: str,
    gate: dict,
    res_map: dict[str, str],
) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """Build edges for a single inline gate node.

    gate -> X  "gates"   for each slug in gate["gates"]   (downstream blocked)
    Y -> gate  "requires" for each slug in gate["requires"] (upstream prereq)

    Unresolvable targets are dropped and counted as dangling.
    """
    edges: list[tuple[str, str, str]] = []
    dangling: list[dict] = []

    for target in gate["gates"]:
        # Resolve target via res_map (covers notes by path/basename and inline
        # gate-ids registered in res_map before edge extraction).
        dst = _resolve_link(target, res_map)
        if dst is None:
            dangling.append({"from": gate_id, "raw_target": target, "type": "gates", "origin": "gate"})
            continue
        if dst == gate_id:
            continue  # self-loop
        edges.append((gate_id, dst, "gates"))

    for prereq in gate["requires"]:
        src = _resolve_link(prereq, res_map)
        if src is None:
            dangling.append({"from": gate_id, "raw_target": prereq, "type": "requires", "origin": "gate"})
            continue
        if src == gate_id:
            continue  # self-loop
        edges.append((src, gate_id, "requires"))

    return edges, dangling


# ---------------------------------------------------------------------------
# Resolution map — wikilink target → path key
# ---------------------------------------------------------------------------

def _build_resolution_map(notes: list[dict]) -> dict[str, str]:
    """Build a mapping from link targets → vault-relative POSIX path (node id).

    Resolution precedence:
    1. Full relpath (without .md): `02-projects/1.0-dev/projalpha`
    2. Basename without extension: `projalpha`
    3. Title field value
    4. Each alias

    Fill-only-if-absent (sorted iteration) → first occurrence wins, deterministic.
    """
    res: dict[str, str] = {}

    def _put(key: str, path: str) -> None:
        if key and key not in res:
            res[key] = path

    for note in sorted(notes, key=lambda n: n["path"]):
        path = note["path"]  # e.g. '02-projects/1.0-dev/projalpha.md'
        fm = note.get("fm") or {}
        raw_text = note.get("text", "")

        # 0. Full path (with .md) — needed so _add() can look up the note's own path.
        _put(path, path)

        # 1. Relpath without extension.
        relpath_no_ext = path[:-3] if path.endswith(".md") else path
        _put(relpath_no_ext, path)

        # 2. Basename without extension.
        basename_no_ext = relpath_no_ext.rsplit("/", 1)[-1]
        _put(basename_no_ext, path)

        # 3. Title.
        title = fm.get("title", "")
        if title:
            _put(title, path)

        # 4. Aliases (parsed from raw text because parse_frontmatter skips lists).
        for alias in _extract_aliases_from_text(raw_text):
            _put(alias, path)

    return res


def _resolve_link(target: str, res_map: dict[str, str]) -> str | None:
    """Resolve a wikilink target string to a node path, or None if dangling.

    Tries (in order): target as-is, target + .md relpath match, basename variants.
    The resolution map already handles both relpath-no-ext and basename keys.
    """
    # Direct map lookup.
    if target in res_map:
        return res_map[target]
    # Try stripping potential leading directory that already exists in map.
    basename = target.rsplit("/", 1)[-1]
    if basename in res_map:
        return res_map[basename]
    return None


# ---------------------------------------------------------------------------
# Edge extraction
# ---------------------------------------------------------------------------

def _extract_edges(
    note: dict,
    res_map: dict[str, str],
) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """Extract directed edges from *note*'s frontmatter edge fields.

    Returns (edges_list, dangling_count).
    Edge tuple = (src, dst, type).

    Edge direction convention (upstream prerequisite → downstream dependent):
      requires: [A] → A → N  "requires"
      blockers: [B] → B → N  "blocked"   (only string items, not dict items)
      up: P      → P → N  "partof"
      supersedes: M → M → N  "supersedes"  (older → newer)
      superseded-by: S → N → S  "supersedes" (normalized to older→newer = N→S... wait:
          spec says superseded-by: S → edge N→S type "supersedes" (older N → newer S)
      related: [R] → N → R  "related"
    """
    path = note["path"]
    raw_text = note.get("text", "")
    fm_text = _extract_fm_text(raw_text)

    edges: list[tuple[str, str, str]] = []
    dangling: list[dict] = []

    def _add(src_raw: str | None, dst_raw: str | None, etype: str) -> None:
        src_r = _resolve_link(src_raw, res_map) if src_raw else None
        dst_r = _resolve_link(dst_raw, res_map) if dst_raw else None
        if src_r is None or dst_r is None:
            if src_raw or dst_raw:
                # Record the endpoint that failed to resolve (the non-self side).
                unresolved = dst_raw if (dst_r is None and dst_raw and dst_raw != path) else src_raw
                dangling.append({"from": path, "raw_target": unresolved, "type": etype, "origin": "frontmatter"})
            return
        if src_r == dst_r:
            return  # Self-loops are meaningless; skip.
        edges.append((src_r, dst_r, etype))

    # --- requires: [A, B] → A→N, B→N  "requires" ---
    for target in _parse_block_list(fm_text, "requires"):
        for t in _split_wikilinks(target) or ([target] if target else []):
            _add(t, path, "requires")

    # --- blockers: [B] — only string entries (not dict entries with text:/severity:) ---
    for target in _parse_block_list(fm_text, "blockers"):
        # _parse_block_list already skips dict items; remaining items are slug refs.
        for t in _split_wikilinks(target) or ([target] if target else []):
            _add(t, path, "blocked")

    # --- up: P → P→N  "partof" ---
    up_val = _parse_scalar(fm_text, "up")
    if up_val:
        for t in _split_wikilinks(up_val):
            _add(t, path, "partof")

    # --- supersedes: M → M→N  "supersedes" ---
    supersedes_val = _parse_scalar(fm_text, "supersedes")
    if supersedes_val:
        for t in _split_wikilinks(supersedes_val):
            _add(t, path, "supersedes")

    # --- superseded-by: S → N→S  "supersedes" (same direction: older→newer) ---
    superseded_by_val = _parse_scalar(fm_text, "superseded-by")
    if superseded_by_val:
        for t in _split_wikilinks(superseded_by_val):
            _add(path, t, "supersedes")

    # --- related: [R] → N→R  "related" ---
    for target in _parse_block_list(fm_text, "related"):
        for t in _split_wikilinks(target) or ([target] if target else []):
            _add(path, t, "related")

    # --- gates: (artifact gate only) N→X "gates"  (this gate blocks X downstream) ---
    # Artifact gates (type: gate) carry their edges in frontmatter like other notes.
    # Their `requires` edges are already extracted above (the field is identical).
    # We only need to add `gates` — the downstream-blocking direction.
    fm = note.get("fm") or {}
    if fm.get("type") == "gate":
        for target in _parse_block_list(fm_text, "gates"):
            for t in _split_wikilinks(target) or ([target] if target else []):
                _add(path, t, "gates")

    return edges, dangling


# ---------------------------------------------------------------------------
# Tarjan SCC (iterative to avoid Python recursion limit on large vaults)
# ---------------------------------------------------------------------------

def _tarjan_sccs(adj: dict[str, list[str]]) -> list[list[str]]:
    """Iterative Tarjan's algorithm — returns list of SCCs (each a list of node ids).

    Nodes not reachable from any root are still assigned an SCC of size 1.
    All nodes in *adj* must appear as keys (including those with empty adjacency lists).
    """
    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[list[str]] = []

    nodes = sorted(adj.keys())  # deterministic order

    # Iterative Tarjan using an explicit call stack.
    # Each frame: (node, iterator-over-neighbors, already-pushed-to-S)
    for start in nodes:
        if start in index:
            continue

        # (node, neighbour_iter, entered)
        call_stack: list[tuple[str, "Iterator[str]", bool]] = []  # type: ignore[type-arg]
        call_stack.append((start, iter(sorted(adj[start])), False))

        while call_stack:
            node, nbr_iter, entered = call_stack[-1]

            if not entered:
                # First visit to node.
                index[node] = index_counter[0]
                lowlink[node] = index_counter[0]
                index_counter[0] += 1
                on_stack[node] = True
                stack.append(node)
                # Mark as entered for next time we pop back to this frame.
                call_stack[-1] = (node, nbr_iter, True)

            try:
                w = next(nbr_iter)
                if w not in index:
                    # w not yet visited — recurse (push frame).
                    call_stack.append((w, iter(sorted(adj[w])), False))
                elif on_stack.get(w):
                    lowlink[node] = min(lowlink[node], index[w])
            except StopIteration:
                # All neighbours processed — pop frame.
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

                # Root of an SCC?
                if lowlink[node] == index[node]:
                    scc: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        scc.append(w)
                        if w == node:
                            break
                    sccs.append(sorted(scc))  # sorted for determinism

    return sccs


# ---------------------------------------------------------------------------
# Back-edge identification via iterative DFS (grey/black colouring)
# ---------------------------------------------------------------------------

def _find_back_edges(adj: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Return the set of all back-edges in *adj* using iterative DFS.

    A back-edge (u, v) is one where v is on the current DFS recursion stack
    (grey) when the edge is first traversed from u.  Removing all back-edges
    yields a DAG (the residual graph for topo ranking).

    Adjacency lists are consumed in sorted order for determinism; start nodes
    are also visited in sorted order.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in adj}
    back_edges: set[tuple[str, str]] = set()

    for start in sorted(adj):
        if colour[start] != WHITE:
            continue

        # Stack frames: (node, neighbour_iterator, entered)
        # 'entered' False = node not yet coloured grey (first visit pending).
        call_stack: list[tuple[str, "Iterator[str]", bool]] = []  # type: ignore[type-arg]
        call_stack.append((start, iter(sorted(adj[start])), False))

        while call_stack:
            node, nbr_iter, entered = call_stack[-1]

            if not entered:
                colour[node] = GREY
                call_stack[-1] = (node, nbr_iter, True)

            try:
                w = next(nbr_iter)
                if colour[w] == GREY:
                    # Back-edge: w is an ancestor on the current DFS path.
                    back_edges.add((node, w))
                elif colour[w] == WHITE:
                    call_stack.append((w, iter(sorted(adj[w])), False))
                # BLACK = already fully processed; cross/forward edge — skip.
            except StopIteration:
                colour[node] = BLACK
                call_stack.pop()

    return back_edges


# ---------------------------------------------------------------------------
# Topological rank (longest-path Kahn on the DAG after back-edge removal)
# ---------------------------------------------------------------------------

def _topo_rank(
    nodes: list[str],
    adj: dict[str, list[str]],
    break_edges: set[tuple[str, str]],
) -> dict[str, int]:
    """Compute longest-path layer rank on the DAG formed by removing break_edges.

    Rank 0 = no incoming edges (root). Rank N = longest path from any root.
    Nodes with no edges get rank 0.
    Uses Kahn's algorithm (BFS-style), choosing longest incoming path.
    """
    # Build in-degree and adjacency ignoring break edges.
    in_deg: dict[str, int] = {n: 0 for n in nodes}
    fwd: dict[str, list[str]] = {n: [] for n in nodes}

    for src in sorted(adj.keys()):
        for dst in sorted(adj[src]):
            if (src, dst) in break_edges:
                continue
            if dst not in in_deg:
                continue  # Dangling dst already filtered out.
            fwd[src].append(dst)
            in_deg[dst] += 1

    rank: dict[str, int] = {n: 0 for n in nodes}
    queue: list[str] = sorted(n for n in nodes if in_deg[n] == 0)
    processed = 0

    while queue:
        node = queue.pop(0)
        processed += 1
        for nbr in sorted(fwd[node]):
            in_deg[nbr] -= 1
            rank[nbr] = max(rank[nbr], rank[node] + 1)
            if in_deg[nbr] == 0:
                queue.append(nbr)
                queue.sort()

    # Any remaining (shouldn't happen after back-edge removal, but guard):
    for n in nodes:
        if n not in rank:
            rank[n] = 0

    return rank


# ---------------------------------------------------------------------------
# Transitive blocker → objective rollup
# ---------------------------------------------------------------------------

_ROLLUP_EDGE_TYPES: frozenset[str] = frozenset({"requires", "advances"})


def _rollup_blockers_to_objectives(
    nodes: list[dict],
    edges: list[tuple[str, str, str]],
) -> None:
    """Compute, for every blocked node, the set of objectives it transitively impedes.

    Mutates node dicts in place:
    - Every node gains ``blocks_objectives``: sorted list of obj:<slug> ids this
      node's blocker transitively impedes via ``requires``/``advances`` edges.
      ``[]`` if the node has no blocker OR reaches no objective.
    - Each objective node (type=="objective") gains ``blocked_by``:
      list of ``{node, text, distance}`` dicts — every upstream blocker that
      reaches it, sorted by ``(distance, node)``.

    Algorithm: BFS forward from each blocked node over ``requires``/``advances``
    edges only.  Per-source ``visited`` set prevents infinite loops in cycles.
    BFS (FIFO) guarantees minimum-hop ``distance`` to each reached objective.
    """
    # Build forward adjacency map over rollup edge types only.
    fwd: dict[str, list[str]] = {}
    for n in nodes:
        fwd.setdefault(n["id"], [])
    for src, dst, etype in edges:
        if etype in _ROLLUP_EDGE_TYPES:
            fwd.setdefault(src, []).append(dst)

    # Index nodes by id for fast mutation.
    by_id: dict[str, dict] = {n["id"]: n for n in nodes}

    # Initialise output keys on all nodes up front (contract: every node gets the key).
    for n in nodes:
        n["blocks_objectives"] = []
        if n.get("type") == "objective":
            n["blocked_by"] = []

    # For each blocked node, BFS forward and collect reached objectives.
    # reverse_index[obj_id] = list of {node, text, distance}
    reverse_index: dict[str, list[dict]] = {}

    for n in nodes:
        blocker_text = n.get("blocker")
        if not blocker_text:
            continue  # no blocker → nothing to propagate

        src_id = n["id"]
        # BFS: deque of (node_id, distance); visited set prevents cycles.
        bfs_queue: collections.deque[tuple[str, int]] = collections.deque([(src_id, 0)])
        visited: set[str] = {src_id}
        reached_objectives: dict[str, int] = {}  # obj_id → min_distance

        while bfs_queue:
            current, dist = bfs_queue.popleft()
            for neighbour in fwd.get(current, []):
                if neighbour in visited:
                    continue
                # Visited marked at ENQUEUE not dequeue — guarantees first discovery == shortest path (min distance).
                visited.add(neighbour)
                neighbour_node = by_id.get(neighbour)
                assert neighbour_node is not None  # fwd is built from nodes; every key resolves
                next_dist = dist + 1
                if neighbour_node.get("type") == "objective":
                    # BFS first-visit == min distance; assign unconditionally (visited guard above ensures single visit).
                    reached_objectives[neighbour] = next_dist
                # Always continue traversal (objectives can advance other objectives).
                bfs_queue.append((neighbour, next_dist))

        n["blocks_objectives"] = sorted(reached_objectives.keys())

        for obj_id, dist in reached_objectives.items():
            reverse_index.setdefault(obj_id, []).append(
                {"node": src_id, "text": blocker_text, "distance": dist}
            )

    # Populate blocked_by on each objective node, sorted by (distance, node).
    # reverse_index keys came from reached_objectives which were validated in BFS above.
    for obj_id, entries in reverse_index.items():
        obj_node = by_id[obj_id]
        obj_node["blocked_by"] = sorted(entries, key=lambda e: (e["distance"], e["node"]))


# ---------------------------------------------------------------------------
# Critical-path-to-objective (_critical_path_to_objective)
# ---------------------------------------------------------------------------


def _critical_path_to_objective(nodes: list[dict]) -> None:
    """For each ULTIMATE objective, identify the single highest-leverage blocker.

    Pre-condition: ``_rollup_blockers_to_objectives`` has already been called —
    every node has ``blocks_objectives`` (sorted list of obj-ids the node's
    blocker transitively impedes) and every objective node has ``blocked_by``
    (list of ``{node, text, distance}`` dicts).

    Mutates ultimate-objective node dicts in place:
    - Adds ``critical_blocker``: ``{node, text, score}`` or ``None``.

    Ranking key (highest wins):
        PRIMARY   : impeded_count = len(blocker_node.blocks_objectives)  — higher = more leverage
        SECONDARY : distance of THIS objective from the blocker            — higher = more foundational
        TIEBREAK  : node id (ascending, i.e. lexicographically smaller wins)

    ``score`` in the result equals the primary key (impeded_count).
    """
    # Build id → node map for fast blocks_objectives lookup.
    by_id: dict[str, dict] = {n["id"]: n for n in nodes}

    def _rank_key(entry: dict) -> tuple:
        # We want: max impeded_count, max distance, min node_id.
        # Use min() with negated primary/secondary so the smallest key wins.
        impeded_count = len(by_id[entry["node"]]["blocks_objectives"])
        return (-impeded_count, -entry["distance"], entry["node"])

    for node in nodes:
        if node.get("type") != "objective" or node.get("kind") != "ultimate":
            continue

        bd: list[dict] = node.get("blocked_by") or []
        if not bd:
            node["critical_blocker"] = None
            continue

        best = min(bd, key=_rank_key)
        impeded_count = len(by_id[best["node"]]["blocks_objectives"])
        node["critical_blocker"] = {
            "node": best["node"],
            "text": best["text"],
            "score": impeded_count,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_graph_facts(
    conn: sqlite3.Connection,
    notes: list[dict],
    vault_root: Path,
    extra_gates: list[dict] | None = None,
    staleness: Mapping[str, str] | None = None,
) -> dict:
    """Build graph tables in *conn* and emit graph.json under *vault_root/00-meta/*.

    Parameters
    ----------
    conn        : open sqlite3 connection (already has the `notes` table).
    notes       : list of dicts with keys 'path', 'fm', 'text' — same dicts
                  assembled inside kb-index.build().
    vault_root  : Path to the vault root (for graph.json output path).
    extra_gates : optional list of gate dicts harvested from project repos by
                  kb-harvest.harvest_gates(). Each dict must have at minimum:
                    id (str), status (str), blocking (bool), gates (list[str]),
                    requires (list[str]), criteria (list[str]).
                  Extra keys (title, source, ref) are used for node labelling.
                  These are merged into the inline_gates dict (vault inline
                  markers take precedence over repo gates for the same gate-id).
    staleness   : optional pre-computed {project_name: state} map where
                  state ∈ {fresh, stale, very_stale, unknown}.  When None,
                  kb-staleness.compute(vault_root) is called once to derive
                  real drift counts from git.  Inject a stub map in tests to
                  keep graph tests hermetic (no live git required).

    Returns
    -------
    dict with stats: node_count, edge_counts (by type), cycle_count,
                     dangling_dropped, gate_count, gate_skipped.
    """
    # ------------------------------------------------------------------ DDL
    conn.execute(_DDL_EDGES)
    conn.execute(_DDL_GRAPH_FACTS)
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM graph_facts")

    # ------------------------------------------------------------------ Build resolution map (notes only first)
    res_map = _build_resolution_map(notes)

    # ------------------------------------------------------------------ Collect all node ids (from notes table)
    all_paths: list[str] = sorted(
        row[0] for row in conn.execute("SELECT path FROM notes")
    )

    # ------------------------------------------------------------------ Collect inline gate nodes
    # Inline gates produce synthetic nodes keyed by gate-id (not a file path).
    # Artifact gates (type:gate) are already path-keyed in all_paths; handled
    # in _extract_edges (gates field) + normal frontmatter flow (requires field).
    inline_gates, gate_count, gate_skipped = _collect_gates(notes)

    # Merge extra_gates (harvested from project repos) into inline_gates.
    # Both artifact and inline repo gates are treated as synthetic nodes here
    # because the repo files are not in the vault's notes table — they have no
    # path-keyed entry. Vault notes take precedence: fill-only-if-absent.
    if extra_gates:
        for g in sorted(extra_gates, key=lambda x: x["id"]):
            gid = g["id"]
            if gid not in inline_gates:
                inline_gates[gid] = {
                    "id": gid,
                    "status": g.get("status", "open"),
                    "blocking": bool(g.get("blocking", False)),
                    "gates": list(g.get("gates", [])),
                    "requires": list(g.get("requires", [])),
                    "ref": g.get("ref"),
                    "criteria": list(g.get("criteria", [])),
                    # Extra fields preserved for node label rendering
                    "title": g.get("title"),
                    "source": g.get("source", "repo"),
                }
                gate_count += 1

    # Inject inline gate-ids into res_map so that edge resolution can find them
    # (both as sources in `gates=` attrs and as targets in other notes' requires).
    for gid in sorted(inline_gates):
        if gid not in res_map:
            res_map[gid] = gid  # gate-id is its own node id

    # ------------------------------------------------------------------ Objectives sidecar (objectives.yaml)
    # Read objectives.yaml BEFORE building the node universe so that obj: node-ids
    # land in all_node_ids (and therefore prereq_adj) for topo/cycle machinery.
    obj_data = _read_objectives(vault_root)
    objective_nodes: dict[str, dict] = {}  # obj_node_id → objective fields

    for slug, fields in sorted(obj_data["objectives"].items()):
        obj_id = f"obj:{slug}"
        objective_nodes[obj_id] = {
            "slug": slug,
            "label": fields["label"],
            "kind": fields["kind"],
            "goal": fields["kind"] == "ultimate",
            "advances": fields["advances"],
        }
        # Register in res_map: obj-id resolves to itself.
        if obj_id not in res_map:
            res_map[obj_id] = obj_id

    # Merged node universe: note paths + inline gate-ids + objective node-ids.
    # Artifact gate paths are already in all_paths.
    all_node_ids: list[str] = sorted(set(all_paths) | set(inline_gates.keys()) | set(objective_nodes.keys()))

    # Two adjacency lists:
    #   prereq_adj — prerequisite edges only (requires/blocked/partof/supersedes/gates/advances)
    #                used for SCC detection, back-edge finding, and topo ranking.
    #   full_adj   — all edge types including related; kept for reference but NOT
    #                fed to graph algorithms (related is associative / undirected).
    prereq_adj: dict[str, list[str]] = {n: [] for n in all_node_ids}

    # ------------------------------------------------------------------ Extract edges (notes + inline gates)
    raw_edges: list[tuple[str, str, str]] = []  # (src, dst, type)
    # Lineage-quality instrumentation (additive; does not affect graph.json shape).
    dangling_records: list[dict] = []           # each unresolved edge: {from, raw_target, type, origin}
    edge_origin: dict[tuple[str, str, str], str] = {}  # (src,dst,type) → producing layer

    # Edges from regular notes (including artifact gate notes).
    for note in sorted(notes, key=lambda n: n["path"]):
        if note["path"] not in prereq_adj:
            continue  # Note was skipped by kb-index (no valid fm), ignore.
        edges, dangling = _extract_edges(note, res_map)
        dangling_records.extend(dangling)
        raw_edges.extend(edges)
        for _e in edges:
            edge_origin.setdefault(_e, "frontmatter")

    # Edges from inline gate nodes.
    for gid in sorted(inline_gates):
        gate = inline_gates[gid]
        edges, dangling = _extract_gate_edges(gid, gate, res_map)
        dangling_records.extend(dangling)
        raw_edges.extend(edges)
        for _e in edges:
            edge_origin.setdefault(_e, "gate")

    # ------------------------------------------------------------------ Sidecar edges (project-edges.yaml)
    # Read operator-authored sidecar and inject edges + goal flags.
    # Direction: sidecar `requires: [Y]` under project X → edge Y→X type "requires"
    # (same convention as frontmatter: Y is prereq of X).
    sidecar = _read_sidecar(vault_root)
    sidecar_goal_paths: set[str] = set()

    for sidecar_key, sidecar_entry in sorted(sidecar.items()):
        # Resolve key: may be a vault-relative path, basename, or title.
        proj_node = _resolve_link(sidecar_key, res_map)
        if proj_node is None:
            # Unresolvable key — skip silently (dangling counting handled below).
            continue

        # goal: collect paths where sidecar says goal: true
        if sidecar_entry.get("goal", False):
            sidecar_goal_paths.add(proj_node)

        # requires edges: each target Y → proj_node "requires"
        for target in sidecar_entry.get("requires", []):
            src_node = _resolve_link(target, res_map)
            if src_node is None:
                dangling_records.append({"from": sidecar_key, "raw_target": target, "type": "requires", "origin": "sidecar"})
                continue
            if src_node == proj_node:
                continue  # self-loop
            _se = (src_node, proj_node, "requires")
            raw_edges.append(_se)
            edge_origin.setdefault(_se, "sidecar")

        # supersedes edges: sidecar `supersedes: [M]` under X → edge M→X "supersedes"
        # Direction: same as frontmatter `supersedes: [[M]]` on note X → _add(M, X, "supersedes")
        # i.e. M (older) → X (newer).
        for target in sidecar_entry.get("supersedes", []):
            src_node = _resolve_link(target, res_map)
            if src_node is None:
                dangling_records.append({"from": sidecar_key, "raw_target": target, "type": "supersedes", "origin": "sidecar"})
                continue
            if src_node == proj_node:
                continue  # self-loop
            _se = (src_node, proj_node, "supersedes")
            raw_edges.append(_se)
            edge_origin.setdefault(_se, "sidecar")

        # partof edges: sidecar `partof: [P]` under X → edge P→X "partof"
        # Direction: same as frontmatter `up: [[P]]` on note X → _add(P, X, "partof")
        # i.e. P (parent) → X (child).
        for target in sidecar_entry.get("partof", []):
            src_node = _resolve_link(target, res_map)
            if src_node is None:
                dangling_records.append({"from": sidecar_key, "raw_target": target, "type": "partof", "origin": "sidecar"})
                continue
            if src_node == proj_node:
                continue  # self-loop
            _se = (src_node, proj_node, "partof")
            raw_edges.append(_se)
            edge_origin.setdefault(_se, "sidecar")

    # ------------------------------------------------------------------ Objectives advances edges
    # Direction: src=advancer (milestone or project) → dst=objective being advanced.
    # milestone→ultimate: for each objective with advances list.
    for obj_id, obj_fields in sorted(objective_nodes.items()):
        for target_slug in obj_fields["advances"]:
            dst_id = f"obj:{target_slug}"
            if dst_id not in objective_nodes:
                # Target slug not in objectives — dangling.
                dangling_records.append({"from": obj_id, "raw_target": dst_id, "type": "advances", "origin": "objectives"})
                continue
            if obj_id == dst_id:
                continue  # self-loop
            _oe = (obj_id, dst_id, "advances")
            raw_edges.append(_oe)
            edge_origin.setdefault(_oe, "objectives")

    # project→objective: for each title in project_advances, resolve via res_map.
    for title, obj_slugs in sorted(obj_data["project_advances"].items()):
        src_node = _resolve_link(title, res_map)
        if src_node is None:
            # Unresolvable project title — count as dangling (one per title, not per slug).
            dangling_records.append({"from": f"project_advances:{title}", "raw_target": title, "type": "advances", "origin": "objectives"})
            continue
        for obj_slug in obj_slugs:
            dst_id = f"obj:{obj_slug}"
            if dst_id not in objective_nodes:
                dangling_records.append({"from": src_node, "raw_target": dst_id, "type": "advances", "origin": "objectives"})
                continue
            if src_node == dst_id:
                continue  # self-loop (shouldn't happen; guard for safety)
            _pe = (src_node, dst_id, "advances")
            raw_edges.append(_pe)
            edge_origin.setdefault(_pe, "objectives")

    # ------------------------------------------------------------------ Deduplicate edges (supersedes dedup; related dedup)
    # Use set for dedup but preserve insertion order within types (sorted).
    seen_edges: set[tuple[str, str, str]] = set()
    deduped_edges: list[tuple[str, str, str]] = []
    for e in sorted(raw_edges):
        if e not in seen_edges:
            seen_edges.add(e)
            deduped_edges.append(e)
            src, dst, etype = e
            # Only prerequisite-type edges go into prereq_adj.
            # Guard: dst must be in the node universe (dangling targets are already
            # counted above; but dedup loop may see dropped edges too — skip).
            if etype in _PREREQ_TYPES and src in prereq_adj and dst in prereq_adj:
                prereq_adj[src].append(dst)

    # ------------------------------------------------------------------ Deduplicate prereq adjacency lists
    for node in prereq_adj:
        prereq_adj[node] = sorted(set(prereq_adj[node]))

    # ------------------------------------------------------------------ Tarjan SCC (prerequisite graph only)
    sccs = _tarjan_sccs(prereq_adj)

    # Assign cycle_id only to SCCs with >1 member; sort SCCs by min member for stable ids.
    cycle_sccs = [scc for scc in sccs if len(scc) > 1]
    cycle_sccs.sort(key=lambda s: s[0])  # stable by first member (already sorted internally)

    node_cycle_id: dict[str, int] = {}
    node_in_cycle: set[str] = set()
    for cid, scc in enumerate(cycle_sccs):
        for node in scc:
            node_cycle_id[node] = cid
            node_in_cycle.add(node)

    # ------------------------------------------------------------------ Find ALL back-edges via iterative DFS
    # Back-edges are edges (u→v) where v is grey (on the DFS stack) when the
    # edge is traversed.  Removing all back-edges yields the residual DAG.
    all_back_edges: set[tuple[str, str]] = _find_back_edges(prereq_adj)

    # ------------------------------------------------------------------ Canonical break edge per SCC (for JSON brk:true)
    # Among all back-edges belonging to a given SCC, choose the lex-min (src,dst)
    # as the canonical UI "break point" badge.  All back-edges get break_edge=1
    # in SQLite; only the canonical one gets brk:true in graph.json.
    scc_canonical_break: dict[int, tuple[str, str]] = {}
    for src, dst in sorted(all_back_edges):
        # The back-edge belongs to the SCC that contains src (or dst).
        # Both endpoints are in a cycle by definition.
        cid = node_cycle_id.get(src)
        if cid is None:
            cid = node_cycle_id.get(dst)
        if cid is None:
            continue  # Safety guard; shouldn't happen for a genuine back-edge.
        if cid not in scc_canonical_break:
            scc_canonical_break[cid] = (src, dst)
        # else: already have a canonical (sorted iteration → first = lex-min)

    # The canonical break-edge set (one per SCC) — used for JSON brk:true.
    canonical_break_edges: set[tuple[str, str]] = set(scc_canonical_break.values())

    # ------------------------------------------------------------------ Topo rank (over full node universe)
    topo = _topo_rank(all_node_ids, prereq_adj, all_back_edges)

    # ------------------------------------------------------------------ Persist edges
    edge_counts: dict[str, int] = {}
    for src, dst, etype in deduped_edges:
        # `cycle` flag: only non-related edges between cycle members are "in cycle".
        if etype != "related" and src in node_in_cycle and dst in node_in_cycle:
            in_cycle = 1
        else:
            in_cycle = 0
        # `break_edge`: 1 only if this edge is a prereq-type back-edge.
        # A `related` edge sharing the same (src,dst) as a prereq back-edge must NOT
        # inherit break_edge=1 — related edges never participate in back-edge analysis.
        is_break = 1 if (etype in _PREREQ_TYPES and (src, dst) in all_back_edges) else 0
        conn.execute(
            "INSERT INTO edges (src, dst, type, cycle, break_edge) VALUES (?, ?, ?, ?, ?)",
            (src, dst, etype, in_cycle, is_break),
        )
        edge_counts[etype] = edge_counts.get(etype, 0) + 1

    # ------------------------------------------------------------------ Persist graph_facts (all nodes)
    for node_id in all_node_ids:
        cid = node_cycle_id.get(node_id)
        trank = topo.get(node_id, 0)
        conn.execute(
            "INSERT OR REPLACE INTO graph_facts (slug, cycle_id, topo_rank) VALUES (?, ?, ?)",
            (node_id, cid, trank),
        )

    # ------------------------------------------------------------------ Emit graph.json (atomic)
    # Gather node metadata from notes' fm dicts.
    fm_by_path: dict[str, dict] = {n["path"]: (n.get("fm") or {}) for n in notes}
    text_by_path: dict[str, str] = {n["path"]: n.get("text", "") for n in notes}

    # ------------------------------------------------------------------ Scout-cache identity map
    # Load language + adr slugs from per-repo scout-cache files (00-meta/scout-cache/*.json).
    # Keyed by repo name (= cache file stem = card title for project notes).
    # Missing directory or malformed files are silently skipped — non-fatal.
    # NOTE: kb-index.py already reads this directory for gates; ideally the identity map
    # would be passed as a parameter (avoids a second directory scan).  Changing the
    # function signature is out of scope here — kb-index.py is not an allowed edit target.
    _scout_language: dict[str, str | None] = {}   # repo_name → language | None
    _scout_adrs: dict[str, list[str]] = {}         # repo_name → list of adr slugs
    _scout_cache_dir = vault_root / "00-meta" / "scout-cache"
    if _scout_cache_dir.is_dir():
        for _cache_file in sorted(_scout_cache_dir.glob("*.json")):
            try:
                _cache = json.loads(_cache_file.read_text(encoding="utf-8"))
                _repo_name = _cache_file.stem  # filename stem is the join key
                _identity = _cache.get("identity") or {}
                _scout_language[_repo_name] = _identity.get("language") or None
                _scout_adrs[_repo_name] = [
                    a["slug"] for a in (_cache.get("adrs") or [])
                    if isinstance(a, dict) and a.get("slug")
                ]
            except Exception:
                pass  # malformed or unreadable — skip silently

    graph_nodes: list[dict] = []

    # --- Note nodes (path-keyed, including artifact gates) ---
    for path in sorted(all_paths):
        fm = fm_by_path.get(path, {})
        raw_text = text_by_path.get(path, "")
        fm_text = _extract_fm_text(raw_text)

        title = fm.get("title") or path
        note_type = fm.get("type") or None
        rag = fm.get("rag-flag") or None
        group = fm.get("group") or None

        # nextsteps: YAML block-list — not in fm dict (parse_frontmatter skips list fields).
        # Read from fm_text via _parse_block_list, same pattern as aliases/tags.
        # nextstep0 is the first element (the canonical "next" action shown on the board).
        # node key "next" is kept unchanged for existing mission-control consumers.
        _nextsteps = _parse_block_list(fm_text, "nextsteps")
        nextstep0: str | None = _nextsteps[0] if _nextsteps else None

        # goal: boolean flag — from frontmatter OR sidecar (either source sets it)
        goal_raw = fm.get("goal", "false")
        goal = str(goal_raw).lower() in ("true", "1", "yes")
        goal = goal or (path in sidecar_goal_paths)

        # blocker: first string text from blockers block (not the slug-ref form)
        blocker_text: str | None = None
        bl_scalar = _parse_scalar(fm_text, "blockers")
        if not bl_scalar.startswith("["):
            # Might be a multi-line block list; grab first text: sub-key.
            m = re.search(
                r"^blockers:\s*$.*?-\s*text:\s*[\"']?(.+?)[\"']?\s*$",
                fm_text, re.MULTILINE | re.DOTALL
            )
            if m:
                blocker_text = m.group(1).strip().strip('"').strip("'")

        # Scout-cache join: language + adr slugs.  Only project nodes have a matching
        # cache file; gate/blocker/adr notes never do, so we gate on note_type.
        # Join key: Path(path).stem matches the scout-cache filename stem.
        _stem = Path(path).stem
        if note_type == "project":
            _language: str | None = _scout_language.get(_stem)
            _adrs: list[str] = _scout_adrs.get(_stem, [])
        else:
            _language = None
            _adrs = []

        node_dict: dict = {
            "id": path,
            "label": title,
            "type": note_type,
            "rag": rag,
            "group": group,
            "topo_rank": topo.get(path, 0),
            "cycle_id": node_cycle_id.get(path),
            "goal": goal,
            "next": nextstep0,
            "nextstep0": nextstep0,
            "objective": _parse_scalar(fm_text, "objective") or fm.get("objective") or None,
            "problem": _parse_scalar(fm_text, "problem") or fm.get("problem") or None,
            "file": _parse_scalar(fm_text, "file") or fm.get("file") or None,
            "language": _language,
            "adrs": _adrs,
            "blocker": blocker_text,
        }

        # Extra fields for artifact gate nodes (type: gate).
        # Read from fm_text (not fm dict) because parse_frontmatter skips list
        # fields — the same reason _extract_aliases_from_text reads from raw text.
        if note_type == "gate":
            # status: scalar string field — safe via fm dict (scalars are preserved).
            node_dict["status"] = _parse_scalar(fm_text, "status") or fm.get("status") or "open"
            # blocking: scalar bool in frontmatter (e.g. `blocking: true`).
            blocking_raw = _parse_scalar(fm_text, "blocking") or str(fm.get("blocking", "false"))
            node_dict["blocking"] = blocking_raw.lower() in ("true", "1", "yes")
            # criteria: block list — count using a direct line-scan from fm_text.
            # _parse_block_list cannot be used here because its `\s*` in the key
            # pattern consumes the newline after `criteria:`, causing the first
            # list item to be mistaken for a scalar value.  Line scan is idiomatic
            # (same pattern as _extract_aliases_from_text which also reads from text).
            node_dict["criteria_count"] = _count_fm_list_items(raw_text, "criteria")

        graph_nodes.append(node_dict)

    # --- Inline gate nodes (synthetic, gate-id keyed) ---
    for gid in sorted(inline_gates):
        gate = inline_gates[gid]
        # Use the title from artifact-gate frontmatter if present; fall back to gate-id.
        label = gate.get("title") or gid
        graph_nodes.append({
            "id": gid,
            "label": label,
            "type": "gate",
            "rag": None,
            "group": None,
            "topo_rank": topo.get(gid, 0),
            "cycle_id": node_cycle_id.get(gid),
            "goal": False,
            "next": None,
            "blocker": None,
            # Gate-specific extras:
            "status": gate["status"],
            "blocking": gate["blocking"],
            "criteria_count": len(gate["criteria"]),
        })

    # --- Objective nodes (synthetic, obj:<slug> keyed) ---
    for obj_id in sorted(objective_nodes):
        obj_fields = objective_nodes[obj_id]
        graph_nodes.append({
            "id": obj_id,
            "label": obj_fields["label"] or obj_fields["slug"],
            "type": "objective",
            "kind": obj_fields["kind"],
            "rag": None,
            "group": None,
            "topo_rank": topo.get(obj_id, 0),
            "cycle_id": node_cycle_id.get(obj_id),
            "goal": obj_fields["goal"],
            "next": None,
            "blocker": None,
        })

    # ------------------------------------------------------------------ Blocker → objective rollup
    # Compute transitive reachability from every blocked node to objectives via
    # requires/advances edges.  Mutates graph_nodes dicts in place so the emitted
    # JSON nodes carry blocks_objectives and (for objectives) blocked_by.
    _rollup_blockers_to_objectives(graph_nodes, deduped_edges)

    # ------------------------------------------------------------------ Critical-path-to-objective
    # For each ultimate objective, identify the single highest-leverage blocker
    # (highest impeded_count, then longest distance, then lex-min node id).
    # Mutates ultimate objective nodes with critical_blocker: {node, text, score} | None.
    _critical_path_to_objective(graph_nodes)

    graph_edges: list[dict] = []
    for src, dst, etype in sorted(deduped_edges):
        # `cycle` in JSON: same rule as table — related edges never cycle-flagged.
        if etype != "related" and src in node_in_cycle and dst in node_in_cycle:
            in_cycle = True
        else:
            in_cycle = False
        # `brk` in JSON: only the one canonical prereq-type back-edge per SCC.
        # A `related` edge sharing the same (src,dst) as a canonical back-edge must
        # not inherit brk:true — related edges are never break-edge candidates.
        is_canonical_break = (etype in _PREREQ_TYPES and (src, dst) in canonical_break_edges)
        graph_edges.append({
            "s": src,
            "d": dst,
            "t": etype,
            "cycle": in_cycle,
            "brk": is_canonical_break,
        })

    graph_json = {"nodes": graph_nodes, "edges": graph_edges}

    out_dir = vault_root / "00-meta"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "graph.json"
    tmp_path_json = out_dir / "graph.json.tmp"

    with open(tmp_path_json, "w", encoding="utf-8") as f:
        json.dump(graph_json, f, ensure_ascii=False, indent=None, separators=(",", ":"))

    os.replace(tmp_path_json, out_path)

    # ------------------------------------------------------------------ Emit lineage-quality.json (atomic)
    # Additive data-quality report (the Accuracy spine): surfaces the silently-dropped
    # dangling edges, per-project freshness, and edge provenance/confidence. Time-varying
    # by design (freshness is relative to `generated_at`) so it is kept OUT of graph.json,
    # which stays data-stable. Rebuildable derivative; gitignored alongside graph.json.
    _now = datetime.now(timezone.utc)

    # Resolve staleness states once per build.  On the live path (staleness=None)
    # this calls kb-staleness.compute(vault_root) via git; in tests, a stub map
    # is injected directly to keep graph tests hermetic.
    # Shape: {project_name_stem: state_string}  where state ∈ {fresh, stale, very_stale, unknown}
    _staleness_states: dict[str, str] = _resolve_staleness_states(staleness, vault_root)

    freshness_counts = {"fresh": 0, "stale": 0, "very_stale": 0, "unknown": 0}
    stale_nodes: list[dict] = []
    for _node in graph_nodes:
        if _node.get("type") != "project":
            continue
        _stem = Path(_node["id"]).stem
        _fr = _staleness_states.get(_stem, "unknown")
        if _fr not in freshness_counts:
            _fr = "unknown"
        freshness_counts[_fr] += 1
        if _fr in ("stale", "very_stale", "unknown"):
            stale_nodes.append({
                "id": _node["id"],
                "label": _node.get("label"),
                "freshness": _fr,
            })
    stale_nodes.sort(key=lambda n: (n["freshness"], n["id"]))

    # Edge provenance + confidence. `confidence: low` is reserved for the only currently
    # LLM/associative-inferred class — `related` edges; everything else is human-authored
    # (sidecar/objectives) or deterministically harvested (frontmatter/gate) = high.
    provenance_counts: dict[str, int] = {}
    low_confidence_edges: list[dict] = []
    for _ge in graph_edges:
        _origin = edge_origin.get((_ge["s"], _ge["d"], _ge["t"]), "frontmatter")
        provenance_counts[_origin] = provenance_counts.get(_origin, 0) + 1
        if _ge["t"] == "related":
            low_confidence_edges.append({"s": _ge["s"], "d": _ge["d"], "t": _ge["t"], "provenance": _origin})

    lineage_quality = {
        "generated_at": _now.isoformat(),
        "summary": {
            "node_count": len(graph_nodes),
            "edge_count": len(graph_edges),
            "dangling_count": len(dangling_records),
            "project_freshness": freshness_counts,
            "edge_provenance": dict(sorted(provenance_counts.items())),
            "low_confidence_edge_count": len(low_confidence_edges),
        },
        "dangling_edges": sorted(
            dangling_records,
            key=lambda r: (r.get("from") or "", r.get("type") or "", str(r.get("raw_target"))),
        ),
        "stale_nodes": stale_nodes,
        "low_confidence_edges": sorted(low_confidence_edges, key=lambda e: (e["s"], e["d"])),
    }
    lq_path = out_dir / "lineage-quality.json"
    lq_tmp = out_dir / "lineage-quality.json.tmp"
    with open(lq_tmp, "w", encoding="utf-8") as f:
        json.dump(lineage_quality, f, ensure_ascii=False, indent=2)
    os.replace(lq_tmp, lq_path)

    stats = {
        "node_count": len(all_paths),
        "edge_counts": edge_counts,
        "cycle_count": len(cycle_sccs),
        "dangling_dropped": len(dangling_records),
        "gate_count": gate_count,
        "gate_skipped": gate_skipped,
    }
    return stats
