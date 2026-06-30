#!/usr/bin/env python3
"""Read-only agent-facing query CLI over kb.sqlite (Spec § 3 / D3).

Provides structured filters (--type, --project, --tool, --severity) and
FTS5 full-text search over the kb.sqlite produced by kb-index.py.  Filters
and FTS terms combine with AND semantics (INTERSECT).

Usage:
    py -3 kb-query.py --db <kb.sqlite> [--type T] [--project P] [--tool X]
                      [--severity S] [--include-stale] [--json] [--with-body]
                      [TEXT ...]

Output (default): compact TSV — path<TAB>type<TAB>project<TAB>title
Output (--json):  JSON array of objects; body included only with --with-body.
Exit codes: 0 = success (including zero rows); 2 = DB missing/unreadable.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_MISSING_MSG = "kb.sqlite not found — run kb-index.py first\n"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open *db_path* read-only.  Raises FileNotFoundError if path is absent."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    # sqlite3 URI needs forward slashes; Path.as_uri() gives file:///C:/... on
    # Windows, so append ?mode=ro manually after stripping the leading file://.
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def _build_query(
    *,
    text: str | None,
    note_type: str | None,
    project: str | None,
    tool: str | None,
    severity: str | None,
    include_stale: bool,
    with_body: bool,
) -> tuple[str, list]:
    """Return (sql, params) for the requested combination of filters + FTS.

    When *text* is provided: FTS JOIN path with notes_fts MATCH.
    When *text* is absent: plain SELECT from notes.
    Structured filters are appended to WHERE as AND clauses in both paths.
    The stale guard (n.stale=0 OR n.stale IS NULL) is default; omitted with
    include_stale=True.

    The body column lives only in notes_fts.  When with_body=True we always
    join notes_fts even on the no-TEXT path so the body column is available.
    """
    params: list = []

    # Columns to select
    if with_body:
        select_cols = "n.path, n.type, n.project, n.title, f.body"
    else:
        select_cols = "n.path, n.type, n.project, n.title"

    # Base FROM / JOIN
    if text is not None:
        # FTS path: notes_fts drives the search; join to notes for filters.
        from_clause = (
            "FROM notes_fts f\n"
            "  JOIN notes n ON n.rowid = f.rowid\n"
        )
        params.append(text)
        where_clauses = ["notes_fts MATCH ?"]
    elif with_body:
        # No FTS text but body requested — still need the join.
        from_clause = (
            "FROM notes n\n"
            "  JOIN notes_fts f ON f.rowid = n.rowid\n"
        )
        where_clauses = []
    else:
        from_clause = "FROM notes n\n"
        where_clauses = []

    # Structured filters
    if note_type is not None:
        where_clauses.append("n.type = ?")
        params.append(note_type)
    if project is not None:
        where_clauses.append("n.project = ?")
        params.append(project)
    if tool is not None:
        where_clauses.append("n.tool = ?")
        params.append(tool)
    if severity is not None:
        where_clauses.append("n.severity = ?")
        params.append(severity)

    # Stale guard
    if not include_stale:
        where_clauses.append("(n.stale = 0 OR n.stale IS NULL)")

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + "\n  AND ".join(where_clauses) + "\n"

    sql = f"SELECT {select_cols}\n{from_clause}{where_sql}ORDER BY n.path"
    return sql, params


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _emit_tsv(rows: list[sqlite3.Row]) -> None:
    """Write compact TSV: path<TAB>type<TAB>project<TAB>title."""
    for row in rows:
        print(
            "\t".join(
                str(v) if v is not None else ""
                for v in (row["path"], row["type"], row["project"], row["title"])
            )
        )


def _emit_json(rows: list[sqlite3.Row], *, with_body: bool) -> None:
    """Write JSON array of objects; include body only when with_body=True."""
    out = []
    for row in rows:
        obj: dict = {
            "path": row["path"],
            "type": row["type"],
            "project": row["project"],
            "title": row["title"],
        }
        if with_body:
            obj["body"] = row["body"]
        out.append(obj)
    print(json.dumps(out, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Query kb.sqlite (produced by kb-index.py) with structured filters "
        "and/or FTS5 full-text search."
    )
    ap.add_argument("--db", required=True, help="Path to kb.sqlite")
    ap.add_argument("--type", dest="note_type", help="Filter by note type")
    ap.add_argument("--project", help="Filter by project")
    ap.add_argument("--tool", help="Filter by tool")
    ap.add_argument("--severity", help="Filter by severity")
    ap.add_argument(
        "--include-stale",
        action="store_true",
        help="Include stale=1 notes (excluded by default)",
    )
    ap.add_argument(
        "--json",
        dest="use_json",
        action="store_true",
        help="Emit JSON array instead of TSV",
    )
    ap.add_argument(
        "--with-body",
        action="store_true",
        help="Include FTS body in output (only effective with --json)",
    )
    ap.add_argument(
        "text",
        nargs="*",
        help="FTS5 search terms (joined with spaces for a MATCH expression)",
    )
    args = ap.parse_args()

    db_path = Path(args.db)

    # Guard: missing DB → exit 2
    if not db_path.exists():
        print(_MISSING_MSG, end="", file=sys.stderr)
        sys.exit(2)

    fts_text: str | None = " ".join(args.text) if args.text else None

    try:
        conn = _open_db(db_path)
        # Probe that this is a valid kb DB (not a zero-byte or alien file).
        # Uses DatabaseError (parent of OperationalError) to also catch
        # "file is not a database" from corrupt/non-SQLite files.
        conn.execute("SELECT 1 FROM notes LIMIT 1")
    except sqlite3.DatabaseError:
        print(_MISSING_MSG, end="", file=sys.stderr)
        sys.exit(2)

    try:
        sql, params = _build_query(
            text=fts_text,
            note_type=args.note_type,
            project=args.project,
            tool=args.tool,
            severity=args.severity,
            include_stale=args.include_stale,
            with_body=args.with_body,
        )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if args.use_json:
        _emit_json(rows, with_body=args.with_body)
    else:
        _emit_tsv(rows)


if __name__ == "__main__":
    main()
