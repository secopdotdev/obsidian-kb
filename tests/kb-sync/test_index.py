"""Tests for kb-index.py — SQLite index builder.

TDD: these tests were written before the implementation to define
the acceptance criteria for the deterministic index build.
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
INDEX_SCRIPT = SKILL / "kb-index.py"
CORPUS_FIX = Path(__file__).resolve().parent / "fixtures" / "corpus"


def run_index(vault: Path, out: Path) -> subprocess.CompletedProcess:
    """Invoke kb-index.py with --vault and --out; return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(INDEX_SCRIPT), "--vault", str(vault), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def copy_corpus(vault: Path) -> None:
    """Copy the fixture corpus notes into the vault root."""
    for src in CORPUS_FIX.iterdir():
        if src.is_file():
            shutil.copy(src, vault / src.name)


def test_note_count(tmp_path):
    """Three corpus notes → three rows in notes table."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, f"build failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert out.exists(), "kb.sqlite was not created"

    conn = sqlite3.connect(str(out))
    try:
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()

    assert count == 3, f"expected 3 rows, got {count}"


def test_fts_match_blocker_body(tmp_path):
    """FTS5 search for a unique term from the blocker body returns the blocker note."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(out))
    try:
        # "purge" appears in the blocker body/title — plain alphanumeric term.
        rows = conn.execute(
            "SELECT title FROM notes_fts WHERE notes_fts MATCH 'purge'"
        ).fetchall()
    finally:
        conn.close()

    titles = [r[0] for r in rows]
    assert any("credential-purge-gap" in t for t in titles), (
        f"expected blocker title in FTS results, got: {titles}"
    )


def test_idempotent_rebuild(tmp_path):
    """Two consecutive builds produce identical notes rows (ORDER BY path)."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"

    # First build
    result1 = run_index(tmp_path, out)
    assert result1.returncode == 0, result1.stderr

    conn1 = sqlite3.connect(str(out))
    try:
        rows1 = conn1.execute(
            "SELECT path, type, project, tool, \"group\", title, status, "
            "severity, severity_rank, stale, last_seen_sha FROM notes ORDER BY path"
        ).fetchall()
    finally:
        conn1.close()

    # Second build — out already exists; must overwrite atomically
    result2 = run_index(tmp_path, out)
    assert result2.returncode == 0, result2.stderr

    conn2 = sqlite3.connect(str(out))
    try:
        rows2 = conn2.execute(
            "SELECT path, type, project, tool, \"group\", title, status, "
            "severity, severity_rank, stale, last_seen_sha FROM notes ORDER BY path"
        ).fetchall()
    finally:
        conn2.close()

    assert rows1 == rows2, "idempotency failure: rows differ between two identical builds"


def test_malformed_note_skipped_not_fatal(tmp_path):
    """A note with no --- frontmatter fence is skipped with a stderr message; build succeeds."""
    copy_corpus(tmp_path)
    # Add a malformed note (no frontmatter fence)
    (tmp_path / "bad-note.md").write_text(
        "# Bad note\n\nNo frontmatter at all.\n", encoding="utf-8"
    )
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, f"build should not fail on malformed note\nstderr={result.stderr}"

    # Still indexed the 3 good notes
    conn = sqlite3.connect(str(out))
    try:
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()

    assert count == 3, f"expected 3 good rows, got {count}"
    # Malformed note reported on stderr
    assert "skip" in result.stderr.lower(), f"expected skip message on stderr, got: {result.stderr!r}"


def test_note_columns(tmp_path):
    """Validate column values for the blocker note row."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(out))
    try:
        row = conn.execute(
            "SELECT type, project, tool, \"group\", title, status, "
            "severity, severity_rank, stale, last_seen_sha FROM notes "
            "WHERE title = 'blk-fixture-credential-purge-gap'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "blocker row not found"
    (note_type, project, tool, group, title, status, severity,
     severity_rank, stale, last_seen_sha) = row

    assert note_type == "blocker"
    assert project == "fixture-project"
    assert tool is None           # blocker uses `project`, not `tool`
    assert severity == "crit"
    assert severity_rank == 0     # bare int from frontmatter
    assert stale == 0             # false → 0
    assert last_seen_sha == "def5678"


def test_excluded_dirs_skipped(tmp_path):
    """Notes inside _templates/, active/, and .obsidian/ are not indexed."""
    copy_corpus(tmp_path)
    for excl_dir in ("_templates", "active", ".obsidian"):
        d = tmp_path / excl_dir
        d.mkdir()
        (d / "should-not-be-indexed.md").write_text(
            "---\ntype: cli\ntitle: \"excluded-note\"\ntool: \"excluded\"\nstale: false\n---\n# excluded\n",
            encoding="utf-8",
        )
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(out))
    try:
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        titles = [r[0] for r in conn.execute("SELECT title FROM notes").fetchall()]
    finally:
        conn.close()

    assert count == 3, f"expected 3 rows (excluded dirs skipped), got {count}; titles={titles}"
    assert "excluded-note" not in titles


def test_project_column_from_tool_field(tmp_path):
    """CLI note: no `project` key → project column populated from `tool` field."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(out))
    try:
        row = conn.execute(
            "SELECT project, tool FROM notes WHERE title = 'cmd-fixture-falcon-rtr-run'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "CLI note not found"
    project, tool = row
    # For a cli note: tool='fixture-tool', no `project` field → project col = 'fixture-tool'
    assert tool == "fixture-tool"
    assert project == "fixture-tool"


def test_atomic_build_uses_temp_file(tmp_path):
    """The build writes to a .tmp file then replaces, leaving no stray .tmp after success."""
    copy_corpus(tmp_path)
    out = tmp_path / "kb.sqlite"
    tmp_file = Path(str(out) + ".tmp")

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    assert out.exists(), "output file should exist after build"
    assert not tmp_file.exists(), ".tmp file should be consumed by os.replace"


def test_retrieval_keywords_indexed_in_fts(tmp_path):
    """retrieval-keywords frontmatter values are searchable via FTS5 BM25."""
    out = tmp_path / "kb.sqlite"

    # Write a project card with unique retrieval-keywords
    note = tmp_path / "test-project.md"
    # Use single-word terms: FTS5 MATCH treats '-' as NOT, so hyphens in search terms
    # would be misinterpreted. The tokenizer also splits on '-', so 'xyzquux' is the
    # searchable token even if the keyword was 'xyzquux-sentinel'.
    note.write_text(
        "---\n"
        "title: test-project\n"
        "type: project\n"
        "status: active\n"
        "retrieval-keywords: ['xyzquuxterm', 'foobarbazterm', 'zorkmagicterm']\n"
        "---\n"
        "# test-project\n"
        "Body text without any keyword.\n",
        encoding="utf-8",
    )

    result = run_index(tmp_path, out)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(out))
    try:
        rows = conn.execute(
            "SELECT title FROM notes_fts WHERE notes_fts MATCH 'xyzquuxterm'"
        ).fetchall()
    finally:
        conn.close()

    titles = [r[0] for r in rows]
    assert "test-project" in titles, (
        f"retrieval-keyword 'xyzquuxterm' should be searchable via FTS5, got: {titles}"
    )
