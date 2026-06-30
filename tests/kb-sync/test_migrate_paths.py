import importlib.util, os
from pathlib import Path
import pytest

MIG_PATH = Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-migrate-paths.py"

def _load():
    spec = importlib.util.spec_from_file_location("mig", MIG_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migrate_rewrites_abs_to_rel(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "C:\\")
    card = tmp_path / "02-projects" / "1.1-dev-tools" / "x.md"
    card.parent.mkdir(parents=True)
    original = "---\ntitle: x\npath: 'C:\\1.1-dev-tools\\x'\nlast-documented-sha: \"abc1234\"\n---\nBody\n"
    card.write_text(original, encoding="utf-8")
    m = _load()
    changed = m.migrate(tmp_path, dry_run=False)
    txt = card.read_text(encoding="utf-8")

    # path line rewritten
    assert "path: 1.1-dev-tools/x" in txt
    # every other byte unchanged — compare full content with expected splice
    expected = original.replace("path: 'C:\\1.1-dev-tools\\x'", "path: 1.1-dev-tools/x")
    assert txt == expected
    # sha and body present (belt-and-suspenders)
    assert 'last-documented-sha: "abc1234"' in txt
    assert "Body" in txt
    # change was reported
    assert len(changed) == 1

    # idempotent: second run makes zero changes
    assert m.migrate(tmp_path, dry_run=False) == []
    # file not touched again
    assert card.read_text(encoding="utf-8") == expected


def test_migrate_already_relative_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "C:\\")
    card = tmp_path / "02-projects" / "misc" / "y.md"
    card.parent.mkdir(parents=True)
    original = "---\ntitle: y\npath: 1.1-dev-tools/y\nlast-documented-sha: \"def5678\"\n---\nContent\n"
    card.write_text(original, encoding="utf-8")
    m = _load()
    changed = m.migrate(tmp_path, dry_run=False)
    # already-relative → not reported, not changed
    assert changed == []
    assert card.read_text(encoding="utf-8") == original


def test_migrate_dry_run_reports_but_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "C:\\")
    card = tmp_path / "02-projects" / "tools" / "z.md"
    card.parent.mkdir(parents=True)
    # Double-quoted YAML scalar: on disk "C:\\1.2-tools\\z" represents C:\1.2-tools\z
    # The script reads raw text, so the regex sees literal double-backslashes; the
    # script must collapse any resulting double-slashes in the relative output.
    original = '---\ntitle: z\npath: "C:\\\\1.2-tools\\\\z"\nlast-documented-sha: "aaa0000"\n---\nText\n'
    card.write_text(original, encoding="utf-8")
    m = _load()
    changed = m.migrate(tmp_path, dry_run=True)
    # change listed
    assert len(changed) == 1
    assert "1.2-tools/z" in changed[0]
    # disk untouched
    assert card.read_text(encoding="utf-8") == original


def test_migrate_preserves_crlf_line_endings(tmp_path, monkeypatch):
    """Non-path lines must survive byte-identical even when the file uses CRLF."""
    monkeypatch.setenv("KB_DEV_ROOT", "C:\\")
    card = tmp_path / "02-projects" / "win" / "w.md"
    card.parent.mkdir(parents=True)
    # write raw bytes so the file genuinely has CRLF
    crlf_content = b"---\r\ntitle: w\r\npath: 'C:\\1.3-win\\w'\r\nlast-documented-sha: \"bbb1111\"\r\n---\r\nWindows body\r\n"
    card.write_bytes(crlf_content)
    m = _load()
    m.migrate(tmp_path, dry_run=False)
    result_bytes = card.read_bytes()
    # CRLF preserved — non-path lines byte-identical
    assert b"\r\n" in result_bytes
    assert b"last-documented-sha: \"bbb1111\"\r\n" in result_bytes
    assert b"Windows body\r\n" in result_bytes
    # path correctly rewritten
    assert b"path: 1.3-win/w" in result_bytes
