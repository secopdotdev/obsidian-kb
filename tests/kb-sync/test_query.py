"""Tests for kb-query.py — agent-facing read-only query CLI over kb.sqlite.

TDD: these tests were written before the implementation to define
the acceptance criteria (red phase). Run kb-query.py → PASS (green phase).
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
INDEX_SCRIPT = SKILL / "kb-index.py"
QUERY_SCRIPT = SKILL / "kb-query.py"
CORPUS_FIX = Path(__file__).resolve().parent / "fixtures" / "corpus"

# A distinctive FTS body term that exists only in the blocker note body.
FTS_TERM = "keyring"

# Stale note markdown — written into tmp_path corpus before indexing so we can
# test default-exclusion and --include-stale without adding a fixture file.
_STALE_NOTE_MD = """\
---
type: project
title: "stale-test-note"
project: "stale-project"
stale: true
---

# stale-test-note

This note is stale and should be excluded by default.
"""


def _build_db(tmp_path: Path) -> Path:
    """Copy corpus, add a stale note, run kb-index.py, return path to kb.sqlite."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # Copy existing corpus notes
    for src in CORPUS_FIX.iterdir():
        if src.is_file():
            shutil.copy(src, vault / src.name)
    # Add a stale note
    (vault / "stale-test-note.md").write_text(_STALE_NOTE_MD, encoding="utf-8")

    out = tmp_path / "kb.sqlite"
    result = subprocess.run(
        [sys.executable, str(INDEX_SCRIPT), "--vault", str(vault), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"kb-index.py failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert out.exists(), "kb.sqlite was not created by kb-index.py"
    return out


def run_query(db: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke kb-query.py with --db and extra args; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(QUERY_SCRIPT), "--db", str(db), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fixture sanity: confirm the blocker row exists and stale row is in DB.
# ---------------------------------------------------------------------------

def test_db_has_four_notes(tmp_path):
    """Fixture build (3 corpus + 1 stale) produces 4 rows in notes."""
    db = _build_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert count == 4, f"expected 4 rows (3 corpus + 1 stale), got {count}"


# ---------------------------------------------------------------------------
# --type + --severity filter
# ---------------------------------------------------------------------------

def test_type_and_severity_filter(tmp_path):
    """--type blocker --severity crit returns only the crit blocker row."""
    db = _build_db(tmp_path)
    result = run_query(db, "--type", "blocker", "--severity", "crit")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 result, got {len(lines)}: {lines}"
    # TSV: path\ttype\tproject\ttitle — check type and title
    parts = lines[0].split("\t")
    assert len(parts) == 4, f"expected 4 TSV columns, got {len(parts)}: {parts}"
    assert parts[1] == "blocker", f"expected type=blocker, got {parts[1]!r}"
    assert "credential-purge-gap" in parts[3], (
        f"expected blocker title, got {parts[3]!r}"
    )


# ---------------------------------------------------------------------------
# FTS term search
# ---------------------------------------------------------------------------

def test_fts_term_returns_match(tmp_path):
    """A positional FTS term returns the note whose body contains that term."""
    db = _build_db(tmp_path)
    result = run_query(db, FTS_TERM)
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 1, f"expected at least 1 FTS result for {FTS_TERM!r}, got 0"
    # The blocker body contains FTS_TERM; its title should appear.
    titles = [ln.split("\t")[3] for ln in lines]
    assert any("credential-purge-gap" in t for t in titles), (
        f"expected blocker title in FTS results, got: {titles}"
    )


# ---------------------------------------------------------------------------
# Stale exclusion / --include-stale
# ---------------------------------------------------------------------------

def test_stale_excluded_by_default(tmp_path):
    """stale-test-note is excluded from results unless --include-stale is set."""
    db = _build_db(tmp_path)
    result = run_query(db)  # no flags — return all non-stale notes
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    titles = [ln.split("\t")[3] for ln in lines if len(ln.split("\t")) >= 4]
    assert "stale-test-note" not in titles, (
        f"stale note appeared in default output: {titles}"
    )
    # Should see the 3 non-stale corpus notes
    assert len(lines) == 3, f"expected 3 non-stale rows, got {len(lines)}: {lines}"


def test_include_stale_shows_stale_note(tmp_path):
    """--include-stale makes the stale-test-note appear in results."""
    db = _build_db(tmp_path)
    result = run_query(db, "--include-stale")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    titles = [ln.split("\t")[3] for ln in lines if len(ln.split("\t")) >= 4]
    assert "stale-test-note" in titles, (
        f"expected stale-test-note in --include-stale output, got: {titles}"
    )
    # Should see all 4 notes
    assert len(lines) == 4, f"expected 4 rows with --include-stale, got {len(lines)}: {lines}"


# ---------------------------------------------------------------------------
# Missing DB → exit 2 + stderr message
# ---------------------------------------------------------------------------

def test_missing_db_exits_2(tmp_path):
    """A nonexistent DB path exits with code 2 and the expected stderr message."""
    missing = tmp_path / "does-not-exist.sqlite"
    result = run_query(missing)
    assert result.returncode == 2, (
        f"expected exit 2 for missing DB, got {result.returncode}"
    )
    assert "kb.sqlite not found" in result.stderr, (
        f"expected 'kb.sqlite not found' in stderr, got: {result.stderr!r}"
    )
    assert "kb-index.py" in result.stderr, (
        f"expected 'kb-index.py' in stderr, got: {result.stderr!r}"
    )


def test_empty_db_exits_2(tmp_path):
    """A zero-byte or table-less SQLite file exits with code 2 and the message."""
    # Table-less SQLite file (valid SQLite header but no notes table)
    empty_db = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(empty_db))
    conn.close()  # creates a valid but table-less SQLite file

    result = run_query(empty_db)
    assert result.returncode == 2, (
        f"expected exit 2 for empty/table-less DB, got {result.returncode}"
    )
    assert "kb.sqlite not found" in result.stderr, (
        f"expected 'kb.sqlite not found' in stderr, got: {result.stderr!r}"
    )

    # Zero-byte file (not a valid SQLite file at all)
    garbage_db = tmp_path / "garbage.sqlite"
    garbage_db.write_bytes(b"this is not a database")

    result2 = run_query(garbage_db)
    assert result2.returncode == 2, (
        f"expected exit 2 for garbage file, got {result2.returncode}"
    )
    assert "kb.sqlite not found" in result2.stderr, (
        f"expected 'kb.sqlite not found' in stderr for garbage file, got: {result2.stderr!r}"
    )


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------

def test_json_output(tmp_path):
    """--json returns a valid JSON array with the expected keys."""
    db = _build_db(tmp_path)
    result = run_query(db, "--type", "blocker", "--json")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    data = json.loads(result.stdout)
    assert isinstance(data, list), f"expected JSON array, got {type(data)}"
    assert len(data) == 1, f"expected 1 blocker row, got {len(data)}"
    row = data[0]
    assert "path" in row
    assert "type" in row
    assert "project" in row
    assert "title" in row
    assert row["type"] == "blocker"
    assert "credential-purge-gap" in row["title"]
    # body should NOT be present unless --with-body
    assert "body" not in row, "body should not appear in JSON without --with-body"


def test_json_with_body(tmp_path):
    """--json --with-body includes a 'body' key in each result object."""
    db = _build_db(tmp_path)
    result = run_query(db, "--type", "blocker", "--json", "--with-body")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    data = json.loads(result.stdout)
    assert len(data) == 1
    row = data[0]
    assert "body" in row, "expected 'body' key in --with-body JSON output"
    # Body content should contain the blocker body text
    assert "purge" in (row["body"] or "").lower(), (
        f"expected 'purge' in body, got: {row['body']!r}"
    )


# ---------------------------------------------------------------------------
# Combined FTS + structured filter (INTERSECT semantics)
# ---------------------------------------------------------------------------

def test_fts_plus_filter_intersection(tmp_path):
    """TEXT + --type filter: only rows matching BOTH are returned."""
    db = _build_db(tmp_path)
    # "purge" is in the blocker body; cli and project notes have no "purge" in body.
    result = run_query(db, "--type", "blocker", "purge")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 intersection result, got {len(lines)}: {lines}"
    parts = lines[0].split("\t")
    assert parts[1] == "blocker"

    # Same FTS term but wrong type — should return 0 rows (not an error)
    result2 = run_query(db, "--type", "cli", "purge")
    assert result2.returncode == 0, f"exit {result2.returncode}: {result2.stderr}"
    lines2 = [ln for ln in result2.stdout.splitlines() if ln.strip()]
    assert len(lines2) == 0, f"expected 0 intersection results, got {len(lines2)}: {lines2}"


# ---------------------------------------------------------------------------
# TSV column shape
# ---------------------------------------------------------------------------

def test_tsv_has_four_columns(tmp_path):
    """Default TSV output has exactly 4 tab-separated columns per row."""
    db = _build_db(tmp_path)
    result = run_query(db)
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) > 0, "expected at least one output row"
    for ln in lines:
        parts = ln.split("\t")
        assert len(parts) == 4, (
            f"expected 4 TSV columns, got {len(parts)}: {parts!r}"
        )


# ---------------------------------------------------------------------------
# --project filter
# ---------------------------------------------------------------------------

def test_project_filter(tmp_path):
    """--project fixture-project returns only notes for that project."""
    db = _build_db(tmp_path)
    result = run_query(db, "--project", "fixture-project")
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 1, "expected at least 1 note for fixture-project"
    for ln in lines:
        parts = ln.split("\t")
        assert parts[2] == "fixture-project", (
            f"expected project=fixture-project, got {parts[2]!r}"
        )


# ---------------------------------------------------------------------------
# Zero results is not an error
# ---------------------------------------------------------------------------

def test_no_match_exits_0(tmp_path):
    """A query with no matching rows exits 0 with empty stdout."""
    db = _build_db(tmp_path)
    result = run_query(db, "--type", "nonexistent-type-xyz")
    assert result.returncode == 0, (
        f"expected exit 0 for no-match query, got {result.returncode}"
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 0, f"expected empty output for no-match, got: {lines}"
