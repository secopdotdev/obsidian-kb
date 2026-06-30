"""Tests for kb-card-write.py render_card() — focused on rag-flag floor enforcement.

The rag-flag floor rule: operator cannot claim green when active blockers say
yellow (med severity) or red (high/crit severity). Operator CAN set red/yellow
conservatively even without blockers.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


_SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_card_write() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kb_card_write", _SKILL / "kb-card-write.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cw = _load_card_write()


def _minimal_merged(blockers: list[dict] | None = None) -> dict:
    return {
        "identity": {
            "repo_url": "https://github.com/example/repo",
            "branch": "main",
            "source_file": "",
            "tier_hint": "",
            "primary_binary": "",
        },
        "blockers": blockers or [],
        "reuse_tags": [],
        "docs_present": [],
        "advances": None,
        "phase": None,
        "milestones": [],
        "cli": [],
        "adrs": [],
        "errors": [],
        "gates": [],
        "summary": "Test project",
        "problem": "Test problem",
        "solution": "Test solution",
        "objective": "Test objective",
        "nextsteps": ["Step 1"],
        "next_command": "make build",
        "file": "",
        "architecture": "Test arch",
    }


def _existing(rag_flag: str = "") -> dict:
    return {
        "rag_flag": rag_flag,
        "status": "active",
        "notes": "",
        "next_command": "",
        "objective": "",
        "problem": "",
        "solution": "",
        "nextsteps": [],
        "file": "",
        "blocker_unblock_map": {},
    }


def _repo(name: str = "myrepo", group: str = "1.1-dev-tools") -> dict:
    return {"name": name, "group": group, "head_sha": "abc123", "path": "1.1-dev-tools/myrepo"}


def _extract_rag_flag(card_text: str) -> str:
    for line in card_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("rag-flag:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def test_rag_flag_operator_green_no_blockers_stays_green(tmp_path: Path) -> None:
    """Operator green with no blockers → green (no floor to apply)."""
    card = cw.render_card(_minimal_merged([]), tmp_path, _repo(), _existing("green"))
    assert _extract_rag_flag(card) == "green"


def test_rag_flag_operator_green_med_blocker_floored_to_yellow(tmp_path: Path) -> None:
    """Operator green but med severity blocker active → floored to yellow."""
    blockers = [{"slug": "test-blocker", "severity": "med", "text": "something pending", "since": "2026-01-01", "unblock": "fix it"}]
    card = cw.render_card(_minimal_merged(blockers), tmp_path, _repo(), _existing("green"))
    assert _extract_rag_flag(card) == "yellow", "green must be floored to yellow when med blocker exists"


def test_rag_flag_operator_green_high_blocker_floored_to_red(tmp_path: Path) -> None:
    """Operator green but high severity blocker active → floored to red."""
    blockers = [{"slug": "critical-blocker", "severity": "high", "text": "broken", "since": "2026-01-01", "unblock": "fix immediately"}]
    card = cw.render_card(_minimal_merged(blockers), tmp_path, _repo(), _existing("green"))
    assert _extract_rag_flag(card) == "red", "green must be floored to red when high blocker exists"


def test_rag_flag_operator_yellow_no_blockers_stays_yellow(tmp_path: Path) -> None:
    """Operator yellow with no blockers → yellow (operator caution respected)."""
    card = cw.render_card(_minimal_merged([]), tmp_path, _repo(), _existing("yellow"))
    assert _extract_rag_flag(card) == "yellow"


def test_rag_flag_operator_red_no_blockers_stays_red(tmp_path: Path) -> None:
    """Operator red with no blockers → red (operator caution respected)."""
    card = cw.render_card(_minimal_merged([]), tmp_path, _repo(), _existing("red"))
    assert _extract_rag_flag(card) == "red"


def test_rag_flag_no_operator_high_blocker_yields_red(tmp_path: Path) -> None:
    """No operator value + high blocker → red (blocker-derived)."""
    blockers = [{"slug": "b1", "severity": "high", "text": "broken", "since": "2026-01-01", "unblock": "fix"}]
    card = cw.render_card(_minimal_merged(blockers), tmp_path, _repo(), _existing(""))
    assert _extract_rag_flag(card) == "red"


def test_rag_flag_no_operator_no_blockers_yields_green(tmp_path: Path) -> None:
    """No operator value + no blockers → green (default)."""
    card = cw.render_card(_minimal_merged([]), tmp_path, _repo(), _existing(""))
    assert _extract_rag_flag(card) == "green"


def test_rag_flag_operator_yellow_high_blocker_floored_to_red(tmp_path: Path) -> None:
    """Operator yellow but high severity blocker → floored to red."""
    blockers = [{"slug": "b1", "severity": "high", "text": "x", "since": "2026-01-01", "unblock": "y"}]
    card = cw.render_card(_minimal_merged(blockers), tmp_path, _repo(), _existing("yellow"))
    assert _extract_rag_flag(card) == "red", "yellow must be floored to red when high blocker exists"


# ---------------------------------------------------------------------------
# Artifact inventory fields in rendered frontmatter
# ---------------------------------------------------------------------------

def _merged_with_artifacts(readme: bool, plan: bool, adrs: int) -> dict:
    m = _minimal_merged()
    m["artifacts"] = {
        "readme_index_exists": readme,
        "plan_file_exists": plan,
        "decision_count": adrs,
    }
    return m


def test_artifact_fields_written_to_frontmatter_true(tmp_path: Path) -> None:
    """When artifacts are all present, frontmatter reflects true/true/count."""
    card = cw.render_card(_merged_with_artifacts(True, True, 3), tmp_path, _repo(), _existing())
    assert "readme_index_exists: true" in card
    assert "plan_file_exists: true" in card
    assert "decision_count: 3" in card


def test_artifact_fields_written_to_frontmatter_false(tmp_path: Path) -> None:
    """When no artifacts present, frontmatter reflects false/false/0."""
    card = cw.render_card(_merged_with_artifacts(False, False, 0), tmp_path, _repo(), _existing())
    assert "readme_index_exists: false" in card
    assert "plan_file_exists: false" in card
    assert "decision_count: 0" in card


def test_artifact_fields_missing_artifacts_key(tmp_path: Path) -> None:
    """When 'artifacts' key is absent from merged, defaults to false/false/0."""
    m = _minimal_merged()
    # No 'artifacts' key at all
    card = cw.render_card(m, tmp_path, _repo(), _existing())
    assert "readme_index_exists: false" in card
    assert "plan_file_exists: false" in card
    assert "decision_count: 0" in card


# ---------------------------------------------------------------------------
# completed_steps and last-sync field tests
# ---------------------------------------------------------------------------

def test_completed_steps_preserved_from_existing(tmp_path: Path) -> None:
    """completed_steps from existing card is written to frontmatter."""
    ex = {**_existing(), "completed_steps": ["Done: ship v1", "Done: write tests"]}
    card = cw.render_card(_minimal_merged(), tmp_path, _repo(), ex)
    assert '"Done: ship v1"' in card
    assert '"Done: write tests"' in card
    assert "completed_steps:" in card


def test_completed_steps_empty_when_not_set(tmp_path: Path) -> None:
    """completed_steps defaults to [] when absent from existing card."""
    card = cw.render_card(_minimal_merged(), tmp_path, _repo(), _existing())
    assert "completed_steps: []" in card


def test_completed_steps_not_overwritten_by_merged(tmp_path: Path) -> None:
    """completed_steps from existing is never replaced by merged (no such key in merged)."""
    ex = {**_existing(), "completed_steps": ["Done: original"]}
    m = {**_minimal_merged(), "completed_steps": ["Should not appear"]}
    card = cw.render_card(m, tmp_path, _repo(), ex)
    assert '"Done: original"' in card
    assert '"Should not appear"' not in card


def test_last_sync_written_to_frontmatter(tmp_path: Path) -> None:
    """last-sync is written to the generator-owned frontmatter block."""
    card = cw.render_card(_minimal_merged(), tmp_path, _repo(), _existing())
    assert "last-sync:" in card
    # Must be a quoted ISO date string (YYYY-MM-DD)
    import re
    assert re.search(r'last-sync: "\d{4}-\d{2}-\d{2}"', card)
