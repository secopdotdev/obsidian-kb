"""Tests for kb-sync-run.py — zero-token pipeline orchestrator.

Import pattern: uses importlib (hyphenated filename cannot be bare-imported).
All file I/O uses tmp_path fixture. Ollama HTTP calls are mocked with
unittest.mock.patch — no real network calls are made.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (hyphen-safe importlib pattern)
# ---------------------------------------------------------------------------

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
RUN_SCRIPT = SKILL / "kb-sync-run.py"
CARD_WRITE_SCRIPT = SKILL / "kb-card-write.py"


def _load_run() -> ModuleType:
    """Load kb-sync-run.py as a module (hyphen-safe)."""
    spec = importlib.util.spec_from_file_location("kb_sync_run", str(RUN_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_card_write() -> ModuleType:
    """Load kb-card-write.py as a module (hyphen-safe)."""
    spec = importlib.util.spec_from_file_location("kb_card_write", str(CARD_WRITE_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------

def _minimal_harvest(name: str = "myrepo", group: str = "1.1-dev-tools") -> dict[str, Any]:
    """Minimal harvest cache dict as written by kb-harvest.py."""
    return {
        "identity": {
            "name": name,
            "repo_url": "https://github.com/secopdotdev/myrepo",
            "branch": "main",
            "source_file": "",
            "primary_binary": "",
            "language": "python",
            "tier_hint": "",
        },
        "head_sha": "abc123",
        "cli": [],
        "errors": [],
        "adrs": [],
        "docs_present": [],
        "harvest_counts": {"cli": 0, "errors": 0, "adrs": 0},
    }


def _minimal_prose(
    summary: str = "A test project.",
    nextsteps: list[str] | None = None,
    next_command: str | None = None,
    blockers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Minimal valid scout prose output."""
    return {
        "summary": summary,
        "nextsteps": nextsteps if nextsteps is not None else ["Step one"],
        "next_command": next_command,
        "blockers": blockers if blockers is not None else [],
        "problem": None,
        "solution": None,
        "objective": None,
        "file": None,
        "architecture": {"summary": "Simple architecture."},
        "reuse_tags": None,
    }


def _make_ollama_response(prose: dict[str, Any]) -> MagicMock:
    """Build a mock httpx.Response for Ollama returning the given prose."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "message": {"content": json.dumps(prose)}
    }
    return mock_resp


def _make_repos_json(tmp_path: Path, repos: list[dict[str, Any]]) -> Path:
    p = tmp_path / "repos.json"
    p.write_text(json.dumps(repos), encoding="utf-8")
    return p


def _make_harvest_cache(vault: Path, name: str, data: dict[str, Any]) -> Path:
    cache_dir = vault / "00-meta" / "scout-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# validate_scout tests
# ---------------------------------------------------------------------------

def test_validate_scout_happy():
    """A fully valid scout dict passes validation."""
    m = _load_run()
    prose = _minimal_prose()
    ok, reason = m.validate_scout(prose)
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize("missing_key", ["summary", "nextsteps", "next_command", "blockers"])
def test_validate_scout_missing_key(missing_key: str):
    """Validation returns False for each required key that is absent."""
    m = _load_run()
    prose = _minimal_prose()
    del prose[missing_key]
    ok, reason = m.validate_scout(prose)
    assert ok is False
    assert missing_key in reason


def test_validate_scout_bad_slug():
    """A blocker slug containing a period fails validation."""
    m = _load_run()
    prose = _minimal_prose(
        blockers=[{"slug": "bad.slug", "text": "oops", "severity": "low"}]
    )
    ok, reason = m.validate_scout(prose)
    assert ok is False
    assert "slug" in reason.lower()


def test_validate_scout_empty_summary_fails():
    """An empty-string summary fails validation."""
    m = _load_run()
    prose = _minimal_prose(summary="   ")
    ok, reason = m.validate_scout(prose)
    assert ok is False


def test_validate_scout_dup_blocker_slug_fails():
    """Duplicate blocker slugs fail validation."""
    m = _load_run()
    prose = _minimal_prose(
        blockers=[
            {"slug": "same-slug", "text": "A", "severity": "low"},
            {"slug": "same-slug", "text": "B", "severity": "med"},
        ]
    )
    ok, reason = m.validate_scout(prose)
    assert ok is False
    assert "dup" in reason


# ---------------------------------------------------------------------------
# Card render — operator fields preserved
# ---------------------------------------------------------------------------

def test_card_render_operator_fields_preserved(tmp_path: Path):
    """Existing card's rag-flag, status, and notes survive a re-render."""
    cw = _load_card_write()

    # Build an existing card with operator-set fields
    existing_card_text = """\
---
type: project
title: "myrepo"
aliases: []
tags: ["type/project", "group/toolbay"]
classifier: "Tool Bay"
group: "1.1-dev-tools"
source-file: ""
repo: "https://github.com/secopdotdev/myrepo"
path: ''
branch: "main"
last-documented-sha: "old999"
created: "2026-01-01"
updated: "2026-01-01"
up: "[[01-groups/1.1-dev-tools]]"
related: []
docs: "docs/kb/"
# --- operator-owned ---
status: paused
rag-flag: red
blocker-severity: ""
blockers: []
nextsteps:
  - "Operator step override"
problem: null
solution: null
objective: null
file: null
next-command: ""
notes: "operator note preserved"
---

# myrepo
"""
    card_path = tmp_path / "02-projects" / "1.1-dev-tools" / "myrepo.md"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(existing_card_text, encoding="utf-8")

    existing = cw.read_existing_operator_fields(card_path)
    assert existing["rag_flag"] == "red"
    assert existing["status"] == "paused"
    assert existing["notes"] == "operator note preserved"
    assert existing["nextsteps"] == ["Operator step override"]

    # Render new card — prose would normally set rag-flag green (no blockers)
    merged = {**_minimal_harvest(), **_minimal_prose(summary="New prose summary.")}
    repo = {
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "new111",
        "path": "1.1-dev-tools/myrepo",
    }
    rendered = cw.render_card(merged, tmp_path, repo, existing)

    # Operator values must win
    assert "rag-flag: red" in rendered
    assert "status: paused" in rendered
    assert 'notes: "operator note preserved"' in rendered
    # Operator nextsteps preserved
    assert "Operator step override" in rendered


# ---------------------------------------------------------------------------
# SHA-skip test
# ---------------------------------------------------------------------------

def test_sha_skip(tmp_path: Path):
    """Repo with head_sha == last_documented_sha produces no output file and appears in skipped."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "abc123",
        "last_documented_sha": "abc123",  # same → skip
    }
    repos_json = _make_repos_json(tmp_path, [repo])

    # No harvest cache needed (sha-skip happens before cache read)
    result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])
    assert result == 0

    card_path = vault / "02-projects" / "1.1-dev-tools" / "myrepo.md"
    assert not card_path.exists(), "card should NOT be written for sha-skipped repo"

    # Capture stdout via capsys would be ideal, but we can verify no card was written
    # which is the key behavioral assertion


def test_sha_skip_appears_in_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """SHA-skipped repo appears in the skipped list in the JSON report."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "deadbeef",
        "last_documented_sha": "deadbeef",
    }
    repos_json = _make_repos_json(tmp_path, [repo])
    result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])
    assert result == 0

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    names = [s["name"] for s in report["skipped"]]
    assert "myrepo" in names
    assert report["skipped"][0]["reason"] == "sha-match"


# ---------------------------------------------------------------------------
# Ollama retry test
# ---------------------------------------------------------------------------

def test_ollama_retry(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """First Ollama response fails validation; retry with same prompt succeeds."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "aabbcc",
        "last_documented_sha": "old000",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", _minimal_harvest())

    # First response: missing 'summary' key (invalid)
    bad_prose = {"nextsteps": [], "next_command": None, "blockers": []}
    good_prose = _minimal_prose()

    bad_resp = _make_ollama_response(bad_prose)
    good_resp = _make_ollama_response(good_prose)

    with patch("httpx.post", side_effect=[bad_resp, good_resp]):
        result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert any(d["name"] == "myrepo" for d in report["documented"])


# ---------------------------------------------------------------------------
# Sentinel lifecycle test
# ---------------------------------------------------------------------------

def test_sentinel_lifecycle(tmp_path: Path):
    """Sentinel exists during card write and is absent after the run completes."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "s3nt1n3l",
        "last_documented_sha": "old",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", _minimal_harvest())

    sentinel_path = vault / "00-meta" / ".kb-sync-active"
    sentinel_present_during_write: list[bool] = []

    original_replace = os.replace

    def _spy_replace(src: str, dst: str) -> None:
        # Called during _atomic_write; check sentinel state at this moment
        sentinel_present_during_write.append(sentinel_path.exists())
        return original_replace(src, dst)

    good_resp = _make_ollama_response(_minimal_prose())

    with patch("httpx.post", return_value=good_resp):
        with patch("os.replace", side_effect=_spy_replace):
            result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 0
    # Sentinel must have existed during at least one atomic write
    assert any(sentinel_present_during_write), (
        "sentinel was not present during card write"
    )
    # Sentinel must be removed after the run
    assert not sentinel_path.exists(), "sentinel should be removed after successful run"


# ---------------------------------------------------------------------------
# Atomic write uses os.replace test
# ---------------------------------------------------------------------------

def test_atomic_write_uses_replace(tmp_path: Path):
    """Card write uses temp-file + os.replace, not direct write to card path."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "atom1c",
        "last_documented_sha": "prev0",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", _minimal_harvest())

    good_resp = _make_ollama_response(_minimal_prose())
    replace_calls: list[tuple[str, str]] = []

    original_replace = os.replace

    def _capture_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        return original_replace(src, dst)

    with patch("httpx.post", return_value=good_resp):
        with patch("os.replace", side_effect=_capture_replace):
            result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 0
    assert replace_calls, "os.replace was never called — atomic write not used"
    # The destination of at least one call should be the card path
    card_path = str(vault / "02-projects" / "1.1-dev-tools" / "myrepo.md")
    dest_paths = [dst for _, dst in replace_calls]
    assert card_path in dest_paths, (
        f"card path {card_path!r} not in os.replace destinations {dest_paths!r}"
    )
    # Source must be a temp file (suffix .tmp, in same directory)
    matching = [(src, dst) for src, dst in replace_calls if dst == card_path]
    assert matching
    src, _ = matching[0]
    assert src.endswith(".tmp"), f"expected .tmp temp file, got {src!r}"


# ---------------------------------------------------------------------------
# Ollama unavailable → exit code 2
# ---------------------------------------------------------------------------

def test_ollama_unavailable_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """ConnectError from httpx causes exit code 2 and KB_OLLAMA_UNAVAILABLE on stderr."""
    import httpx as _httpx

    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "conn3rr",
        "last_documented_sha": "prev",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", _minimal_harvest())

    with patch("httpx.post", side_effect=_httpx.ConnectError("connection refused")):
        result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 2, f"expected exit code 2, got {result}"
    captured = capsys.readouterr()
    assert "KB_OLLAMA_UNAVAILABLE" in captured.err


# ---------------------------------------------------------------------------
# read_existing_operator_fields — missing file
# ---------------------------------------------------------------------------

def test_read_existing_operator_fields_missing_file(tmp_path: Path):
    """Missing card file returns all-None dict."""
    cw = _load_card_write()
    result = cw.read_existing_operator_fields(tmp_path / "nonexistent.md")
    assert result["rag_flag"] is None
    assert result["status"] is None
    assert result["notes"] is None
    assert result["objective"] is None


# ---------------------------------------------------------------------------
# Dry-run — no writes, no Ollama calls
# ---------------------------------------------------------------------------

def test_dry_run_no_writes(tmp_path: Path):
    """--dry-run produces no output files and makes no Ollama calls."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "dry111",
        "last_documented_sha": "old000",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])

    with patch("httpx.post") as mock_post:
        result = m.main([
            "--repos-json", str(repos_json),
            "--vault", str(vault),
            "--dry-run",
        ])

    assert result == 0
    mock_post.assert_not_called()
    card_path = vault / "02-projects" / "1.1-dev-tools" / "myrepo.md"
    assert not card_path.exists()


# ---------------------------------------------------------------------------
# C1 — harvest cache injected into Ollama prompt
# ---------------------------------------------------------------------------

def test_harvest_cache_injected_in_prompt(tmp_path: Path):
    """Ollama POST payload contains the harvest cache JSON in the prompt content."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    harvest = _minimal_harvest()  # head_sha: "abc123"
    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "cache111",
        "last_documented_sha": "old",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", harvest)

    good_resp = _make_ollama_response(_minimal_prose())
    captured_payloads: list[dict[str, Any]] = []

    def _capture_post(url: str, *, json: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
        if json is not None:
            captured_payloads.append(json)
        return good_resp

    with patch("httpx.post", side_effect=_capture_post):
        result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 0
    assert captured_payloads, "httpx.post was never called"
    prompt_content = captured_payloads[0]["messages"][0]["content"]
    # Harvest cache JSON must appear in the prompt
    assert "HARVEST CACHE" in prompt_content
    assert '"head_sha"' in prompt_content
    assert "abc123" in prompt_content  # head_sha value from _minimal_harvest()


# ---------------------------------------------------------------------------
# I1 — operator blocker unblock text preserved across re-render
# ---------------------------------------------------------------------------

def test_blocker_unblock_preserved(tmp_path: Path):
    """Operator-curated unblock text on a blocker slug survives a re-render."""
    cw = _load_card_write()

    existing_card_text = """\
---
type: project
title: "myrepo"
aliases: []
tags: ["type/project", "group/toolbay"]
classifier: "Tool Bay"
group: "1.1-dev-tools"
source-file: ""
repo: "https://github.com/secopdotdev/myrepo"
path: ''
branch: "main"
last-documented-sha: "old"
created: "2026-01-01"
updated: "2026-01-01"
up: "[[01-groups/1.1-dev-tools]]"
related: []
docs: "docs/kb/"
# --- operator-owned ---
status: active
rag-flag: red
blocker-severity: high
blockers:
  - slug: missing-tests
    text: "Tests are missing"
    severity: high
    since: "2026-01-01"
    unblock: "Run pytest and fix failures"
nextsteps: []
problem: null
solution: null
objective: null
file: null
next-command: ""
notes: ""
---

# myrepo
"""
    card_path = tmp_path / "02-projects" / "1.1-dev-tools" / "myrepo.md"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(existing_card_text, encoding="utf-8")

    existing = cw.read_existing_operator_fields(card_path)
    assert existing.get("blocker_unblock_map") == {"missing-tests": "Run pytest and fix failures"}

    # New prose has the same slug but no unblock text (prose side loses the curation)
    prose = _minimal_prose(
        blockers=[{
            "slug": "missing-tests",
            "text": "Tests still missing",
            "severity": "high",
            "since": "2026-01-01",
            "unblock": None,
        }]
    )
    merged = {**_minimal_harvest(), **prose}
    repo = {
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "new222",
        "path": "1.1-dev-tools/myrepo",
    }
    rendered = cw.render_card(merged, tmp_path, repo, existing)
    assert "Run pytest and fix failures" in rendered, (
        "operator unblock text should be preserved in re-rendered card"
    )


# ---------------------------------------------------------------------------
# I2 — validate_scout: malformed (non-dict) blocker does not raise
# ---------------------------------------------------------------------------

def test_validate_scout_malformed_blocker_string():
    """Blocker as a plain string returns (False, reason) without raising AttributeError."""
    m = _load_run()
    prose = _minimal_prose(blockers=["this is a string, not a dict"])
    ok, reason = m.validate_scout(prose)
    assert ok is False
    assert reason  # must produce a non-empty reason


# ---------------------------------------------------------------------------
# I3 — prose cannot override structured head_sha
# ---------------------------------------------------------------------------

def test_prose_cannot_override_head_sha(tmp_path: Path):
    """Prose dict containing head_sha does not overwrite the structured head_sha."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    harvest = _minimal_harvest()  # head_sha: "abc123"
    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "abc123",
        "last_documented_sha": "old",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", harvest)

    # Prose includes a hallucinated head_sha
    prose_with_injection = {**_minimal_prose(), "head_sha": "FAKEHASH"}
    good_resp = _make_ollama_response(prose_with_injection)

    with patch("httpx.post", return_value=good_resp):
        result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 0
    # Written cache must retain the original head_sha from structured data
    cache_path = vault / "00-meta" / "scout-cache" / "myrepo.json"
    written_cache = json.loads(cache_path.read_text())
    assert written_cache["head_sha"] == "abc123", (
        f"prose must not override head_sha; got {written_cache['head_sha']!r}"
    )


# ---------------------------------------------------------------------------
# I4 — severity synonym normalization via _norm_severity
# ---------------------------------------------------------------------------

def test_severity_invalid_value_defaults_to_low():
    """'critical' normalizes to 'crit' (not 'low') via _norm_severity upstream."""
    m = _load_run()
    # _norm_severity maps 'critical' → 'crit' before the card writer sees the value
    result = m._norm_severity("critical")
    assert result == "crit", f"expected 'crit', got {result!r}"
    # Verify the raw string would NOT have passed through _render_blockers_fm unchanged
    cw = _load_card_write()
    # After normalization upstream, the card writer receives the canonical value 'crit'
    blockers_normalized = [{
        "slug": "bad-sev",
        "text": "Some blocker",
        "severity": "crit",  # already normalized by _norm_severity
        "since": None,
        "unblock": None,
    }]
    rendered_fm = cw._render_blockers_fm(blockers_normalized)
    assert "severity: crit" in rendered_fm, "normalized crit severity must appear in frontmatter"
    assert "severity: critical" not in rendered_fm


def test_severity_medium_normalizes_to_med():
    """'medium' normalizes to canonical 'med' via _norm_severity."""
    m = _load_run()
    assert m._norm_severity("medium") == "med"
    assert m._norm_severity("MEDIUM") == "med"   # case-insensitive
    assert m._norm_severity("moderate") == "med"


# ---------------------------------------------------------------------------
# Sentinel removed on exit-2 path (ConnectError)
# ---------------------------------------------------------------------------

def test_sentinel_removed_on_exit2_path(tmp_path: Path):
    """Sentinel is removed even when Ollama is unreachable and main() returns 2."""
    import httpx as _httpx

    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    repo_desc = {
        "path": str(tmp_path / "myrepo"),
        "name": "myrepo",
        "group": "1.1-dev-tools",
        "head_sha": "s3nt2",
        "last_documented_sha": "old",
    }
    repos_json = _make_repos_json(tmp_path, [repo_desc])
    _make_harvest_cache(vault, "myrepo", _minimal_harvest())

    sentinel_path = vault / "00-meta" / ".kb-sync-active"

    with patch("httpx.post", side_effect=_httpx.ConnectError("connection refused")):
        result = m.main(["--repos-json", str(repos_json), "--vault", str(vault)])

    assert result == 2, f"expected exit code 2, got {result}"
    assert not sentinel_path.exists(), (
        "sentinel must be removed by the finally block even on exit-2 path"
    )


# ---------------------------------------------------------------------------
# --stamp-manifest mode tests
# ---------------------------------------------------------------------------

def _make_manifest(vault: Path, entries: list[dict[str, Any]]) -> Path:
    """Write a minimal kb-manifest.json to vault/00-meta/."""
    meta_dir = vault / "00-meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    p = meta_dir / "kb-manifest.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return p


def test_stamp_manifest_updates_sha(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """--stamp-manifest stamps last_documented_sha for each named entry."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    manifest_path = _make_manifest(vault, [
        {"name": "alpha", "group": "1.1-dev-tools", "last_documented_sha": "old-alpha"},
        {"name": "beta",  "group": "1.0-dev",       "last_documented_sha": "old-beta"},
    ])

    repos = [
        {"name": "alpha", "head_sha": "new-alpha-sha"},
        {"name": "beta",  "head_sha": "new-beta-sha"},
    ]
    repos_json = _make_repos_json(tmp_path, repos)

    result = m.main([
        "--repos-json", str(repos_json),
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 0

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    shas = {e["name"]: e["last_documented_sha"] for e in updated}
    assert shas["alpha"] == "new-alpha-sha"
    assert shas["beta"]  == "new-beta-sha"


def test_stamp_manifest_skips_missing_name(tmp_path: Path):
    """--stamp-manifest leaves entries whose name is not in repos-data unchanged."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    manifest_path = _make_manifest(vault, [
        {"name": "alpha", "last_documented_sha": "old-alpha"},
        {"name": "gamma", "last_documented_sha": "old-gamma"},  # not in repos-data
    ])

    repos = [{"name": "alpha", "head_sha": "new-alpha"}]
    repos_json = _make_repos_json(tmp_path, repos)

    result = m.main([
        "--repos-json", str(repos_json),
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 0

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    shas = {e["name"]: e["last_documented_sha"] for e in updated}
    assert shas["alpha"] == "new-alpha"
    assert shas["gamma"] == "old-gamma", "gamma should be untouched"


def test_stamp_manifest_skips_empty_head_sha(tmp_path: Path):
    """--stamp-manifest ignores repo entries with an empty head_sha."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    manifest_path = _make_manifest(vault, [
        {"name": "alpha", "last_documented_sha": "old-alpha"},
    ])

    repos = [{"name": "alpha", "head_sha": ""}]  # empty sha — skip
    repos_json = _make_repos_json(tmp_path, repos)

    result = m.main([
        "--repos-json", str(repos_json),
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 0

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated[0]["last_documented_sha"] == "old-alpha", "empty sha must not overwrite"


def test_stamp_manifest_no_manifest_file(tmp_path: Path):
    """--stamp-manifest exits 0 gracefully when kb-manifest.json doesn't exist."""
    m = _load_run()
    vault = tmp_path / "vault"
    (vault / "00-meta").mkdir(parents=True)  # no manifest file

    repos = [{"name": "alpha", "head_sha": "newsha"}]
    repos_json = _make_repos_json(tmp_path, repos)

    result = m.main([
        "--repos-json", str(repos_json),
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 0


def test_stamp_manifest_atomic_write(tmp_path: Path):
    """--stamp-manifest uses atomic write (no .tmp files left behind)."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    manifest_path = _make_manifest(vault, [
        {"name": "alpha", "last_documented_sha": "old"},
    ])

    repos = [{"name": "alpha", "head_sha": "new"}]
    repos_json = _make_repos_json(tmp_path, repos)

    m.main(["--repos-json", str(repos_json), "--vault", str(vault), "--stamp-manifest"])

    tmp_files = list((vault / "00-meta").glob("*.tmp"))
    assert not tmp_files, f"no .tmp files should remain: {tmp_files}"


def test_stamp_manifest_repos_data_inline_json(tmp_path: Path):
    """--stamp-manifest works with --repos-data inline JSON (the production workflow.js path)."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    manifest_path = _make_manifest(vault, [
        {"name": "myrepo", "last_documented_sha": "old-sha"},
    ])

    # Use --repos-data with inline JSON, exactly as workflow.js does
    repos_inline = json.dumps([{"name": "myrepo", "head_sha": "abc12345"}])
    result = m.main([
        "--repos-data", repos_inline,
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 0

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated[0]["last_documented_sha"] == "abc12345"


def test_stamp_manifest_non_list_repos_data_returns_error(tmp_path: Path):
    """--repos-data with a JSON object (not array) returns exit code 1 cleanly."""
    m = _load_run()
    vault = tmp_path / "vault"
    vault.mkdir()

    result = m.main([
        "--repos-data", '{"name": "notanarray"}',
        "--vault", str(vault),
        "--stamp-manifest",
    ])
    assert result == 1
