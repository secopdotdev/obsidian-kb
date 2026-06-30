#!/usr/bin/env python3
"""Deterministic SQLite index builder for the kb-sync knowledge base (Spec § 3 / D3).

Scans the Obsidian vault's markdown notes, parses YAML frontmatter, and writes
kb.sqlite containing:
  - notes(path, type, project, tool, "group", title, status, severity,
          severity_rank, stale, last_seen_sha)
  - notes_fts  FTS5 virtual table over (title, body)

Build is ATOMIC (temp → os.replace) and IDEMPOTENT: a second build over an
unchanged corpus yields identical row content.

Usage:
    py -3 kb-index.py --vault <vault-root> --out <vault-root>/00-meta/kb.sqlite
"""
import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Graph-facts pass lives in kb-graph.py (hyphenated name — load via importlib).
def _import_kb_graph():
    """Lazily import kb-graph module from the same directory as this file."""
    spec = importlib.util.spec_from_file_location(
        "kb_graph", Path(__file__).with_name("kb-graph.py")
    )
    assert spec is not None and spec.loader is not None, "kb-graph.py not found"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

# Directories (top-level path parts) to skip during the corpus scan.
SKIP_DIRS: frozenset[str] = frozenset({"_templates", "active", ".obsidian", "web"})

# DDL for the main structured table.
_CREATE_NOTES = """\
CREATE TABLE notes (
    path         TEXT PRIMARY KEY,
    type         TEXT,
    project      TEXT,
    tool         TEXT,
    "group"      TEXT,
    title        TEXT,
    status       TEXT,
    severity     TEXT,
    severity_rank INTEGER,
    stale        INTEGER NOT NULL DEFAULT 0,
    last_seen_sha TEXT,
    tags         TEXT
)
"""

# DDL for the FTS5 full-text virtual table.
_CREATE_FTS = """\
CREATE VIRTUAL TABLE notes_fts USING fts5(
    title,
    body
)
"""


# ---------------------------------------------------------------------------
# Frontmatter parser — line-scan only (stdlib; no PyYAML dependency).
# Returns the frontmatter section (lines between the two `---` fences) as a
# dict of flat string values, or None if no valid `---` fence is present.
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> dict | None:
    """Parse the YAML frontmatter block from *text*; return a flat dict or None.

    Recognises the leading `---`-fenced block only. Keys with quoted scalar
    values (`"value"` or `'value'`) are unquoted; bare integers and booleans
    are kept as strings (the caller coerces). Returns None if the fence is
    absent or unterminated — the note is skipped by the caller.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fm_lines: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        fm_lines.append(ln)
    else:
        # Reached end of file without a closing `---` — unterminated fence.
        return None

    result: dict[str, str] = {}
    for ln in fm_lines:
        if ":" not in ln:
            continue
        key, _, raw = ln.partition(":")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        # Skip list/block-scalar lines that start a YAML multi-value field.
        raw = raw.strip()
        # Skip YAML inline lists (aliases, tags, etc.) — not needed for index fields.
        if raw.startswith("[") or raw.startswith("-"):
            continue
        # Strip surrounding quotes (single or double).
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1]
            # Unescape backslash-escaped double-quotes written by _yaml_str.
            raw = raw.replace('\\"', '"').replace("\\\\", "\\")
        result[key] = raw

    return result


def _extract_tags(text: str) -> str | None:
    """Return frontmatter `tags` as a JSON-array string, or None.

    The flat parse_frontmatter() skips list fields; tags drive ties/discover
    retrieval, so capture them here. Handles inline (`tags: [a, "b"]`), block
    (`tags:` then `- a`), and scalar (`tags: a`) forms.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    n = len(lines)
    tags: list[str] = []
    i = 1
    while i < n and lines[i].strip() != "---":
        stripped = lines[i].strip()
        if stripped.startswith("tags:"):
            val = stripped[len("tags:"):].strip()
            if val.startswith("[") and val.endswith("]"):                 # inline list
                for part in val[1:-1].split(","):
                    t = part.strip().strip('"').strip("'")
                    if t:
                        tags.append(t)
            elif val in ("", "[]"):                                       # block list
                j = i + 1
                while j < n and lines[j].strip() != "---":
                    s = lines[j].strip()
                    if s.startswith("- "):
                        t = s[2:].strip().strip('"').strip("'")
                        if t:
                            tags.append(t)
                    elif s and ":" in s and not s.startswith("#"):
                        break                                            # next frontmatter key
                    j += 1
            else:                                                        # scalar
                t = val.strip('"').strip("'")
                if t:
                    tags.append(t)
            break
        i += 1
    return json.dumps(tags) if tags else None


def _extract_body(text: str) -> str:
    """Return everything after the closing `---` frontmatter fence as body text.

    Used for FTS indexing. Returns the whole text if no fence is found.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    in_fm = True
    body_lines: list[str] = []
    for ln in lines[1:]:
        if in_fm:
            if ln.strip() == "---":
                in_fm = False
        else:
            body_lines.append(ln)
    return "\n".join(body_lines)


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def build(vault: Path, out: Path) -> None:
    """Scan *vault*, build kb.sqlite at *out* (atomic temp → replace).

    Skips notes inside SKIP_DIRS. Logs malformed notes to stderr and continues.
    After all notes are indexed, calls build_graph_facts() from kb-graph.py to
    populate the `edges` and `graph_facts` tables and emit 00-meta/graph.json.
    """
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(str(out) + ".tmp")
    # Remove any leftover tmp from a prior aborted run before opening.
    if tmp.exists():
        tmp.unlink()

    conn = sqlite3.connect(str(tmp))

    # Accumulate successfully-indexed notes for the graph pass.
    indexed_notes: list[dict] = []

    try:
        conn.execute(_CREATE_NOTES)
        conn.execute(_CREATE_FTS)

        for md_path in sorted(vault.glob("**/*.md")):
            # Skip excluded top-level dirs by path parts.
            try:
                rel = md_path.relative_to(vault)
            except ValueError:
                continue
            if set(rel.parts) & SKIP_DIRS:
                continue

            text = md_path.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            if fm is None:
                print(f"skip {rel.as_posix()}: no valid frontmatter fence", file=sys.stderr)
                continue

            # Derive column values from frontmatter keys.
            note_type = fm.get("type") or None
            tool_val = fm.get("tool") or None
            project_val = fm.get("project") or None
            # `project` column: frontmatter `project` OR `tool` (whichever present).
            project_col = project_val or tool_val
            group_val = fm.get("group") or None
            title_val = fm.get("title") or None
            status_val = fm.get("status") or None
            severity_val = fm.get("severity") or None

            # severity-rank: bare int in frontmatter (no quotes), or NULL.
            sev_rank_raw = fm.get("severity-rank")
            severity_rank: int | None = None
            if sev_rank_raw is not None:
                try:
                    severity_rank = int(sev_rank_raw)
                except (ValueError, TypeError):
                    severity_rank = None

            # stale: true → 1, anything else (false / absent) → 0.
            stale_raw = fm.get("stale", "false")
            stale: int = 1 if str(stale_raw).lower() == "true" else 0

            # last_seen_sha: prefer `last-documented-sha`, fall back to `last-seen`.
            last_sha = fm.get("last-documented-sha") or fm.get("last-seen") or None

            # Store path as vault-relative POSIX for portability.
            path_key = rel.as_posix()

            # Derive body text for FTS.
            body = _extract_body(text)
            # Inject retrieval-keywords into the FTS body so BM25 can match on them.
            # parse_frontmatter skips inline lists, so we extract with a targeted regex.
            # Format written by kb-card-write.py: retrieval-keywords: ['term1', 'term2']
            # Scope the regex to the frontmatter block only (between the two --- fences) to
            # prevent body lines that happen to start with 'retrieval-keywords:' from injecting
            # spurious terms (e.g. convention docs showing YAML examples in their body).
            _fm_block = ""
            if text.startswith("---"):
                _fence_end = text.find("\n---", 3)
                if _fence_end > 0:
                    _fm_block = text[3:_fence_end]
            _rk_match = re.search(r"^retrieval-keywords:\s*\[([^\]]*)\]", _fm_block, re.MULTILINE)
            if _rk_match:
                _kw_terms = [t.strip().strip("'\"") for t in _rk_match.group(1).split(",")]
                _kw_terms = [t for t in _kw_terms if t]
                if _kw_terms:
                    body += "\n" + " ".join(_kw_terms)
            # tags as JSON array (list-aware; the flat parser skips list fields).
            tags_json = _extract_tags(text)

            try:
                cur = conn.execute(
                    "INSERT INTO notes "
                    "(path, type, project, tool, \"group\", title, status, "
                    "severity, severity_rank, stale, last_seen_sha, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        path_key,
                        note_type,
                        project_col,
                        tool_val,
                        group_val,
                        title_val,
                        status_val,
                        severity_val,
                        severity_rank,
                        stale,
                        last_sha,
                        tags_json,
                    ),
                )
            except sqlite3.Error as exc:
                print(f"skip {path_key}: notes insert error: {exc}", file=sys.stderr)
                continue
            # Pair the FTS row to the notes row by EXPLICIT rowid (not parallel
            # auto-rowid): if an FTS insert ever fails, drop the orphan notes row so
            # notes.rowid <-> notes_fts.rowid can never desync for later rows.
            rid = cur.lastrowid
            try:
                conn.execute(
                    "INSERT INTO notes_fts (rowid, title, body) VALUES (?, ?, ?)",
                    (rid, title_val or "", body),
                )
            except sqlite3.Error as exc:
                conn.execute("DELETE FROM notes WHERE rowid = ?", (rid,))
                print(f"skip {path_key}: fts insert error: {exc}", file=sys.stderr)
                continue

            # Accumulate for the graph pass (path + flat fm + raw text).
            indexed_notes.append({"path": path_key, "fm": fm, "text": text})

        # --- Graph-facts pass (runs after all notes are indexed) ---
        try:
            # Load repo gates from scout-cache files (Design A: harvest→index→graph).
            # The canonical cache location is <vault>/00-meta/scout-cache/ (same path
            # used by kb-atomize.py). Missing directory → empty list (non-fatal).
            extra_gates: list[dict] = []
            scout_cache_dir = vault / "00-meta" / "scout-cache"
            if scout_cache_dir.is_dir():
                for cache_file in sorted(scout_cache_dir.glob("*.json")):
                    try:
                        import json as _json
                        cache = _json.loads(cache_file.read_text(encoding="utf-8"))
                        repo_gates = cache.get("gates")
                        if isinstance(repo_gates, list):
                            extra_gates.extend(repo_gates)
                    except Exception as exc:
                        print(
                            f"graph: skip scout-cache {cache_file.name}: {exc}",
                            file=sys.stderr,
                        )

            kb_graph = _import_kb_graph()
            stats = kb_graph.build_graph_facts(conn, indexed_notes, vault, extra_gates)
            gate_info = f", {stats.get('gate_count', 0)} gates"
            print(
                f"graph: {stats['node_count']} nodes, "
                + ", ".join(f"{v} {k}" for k, v in sorted(stats["edge_counts"].items()))
                + f", {stats['cycle_count']} cycles, "
                f"{stats['dangling_dropped']} dangling"
                + gate_info,
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover — guard so index build never aborts
            print(f"graph pass error (non-fatal): {exc}", file=sys.stderr)

        conn.commit()
    finally:
        conn.close()

    # Atomic replace — conn is closed above so Windows won't block.
    os.replace(tmp, out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build kb.sqlite from the Obsidian vault markdown corpus."
    )
    ap.add_argument("--vault", required=True, help="Root of the Obsidian vault")
    ap.add_argument(
        "--out",
        required=True,
        help="Output path for kb.sqlite (e.g. <vault>/00-meta/kb.sqlite)",
    )
    args = ap.parse_args()

    vault = Path(args.vault)
    out = Path(args.out)

    if not vault.is_dir():
        print(f"error: vault directory does not exist: {vault}", file=sys.stderr)
        sys.exit(1)

    build(vault, out)


if __name__ == "__main__":
    main()
