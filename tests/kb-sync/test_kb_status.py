"""Tests for kb-status.py — offline KB status view.

Strategy: hermetic (no real git, no real vault required).
  - Inject a synthetic staleness map directly into build_status().
  - Fake lineage-quality.json written to tmp_path for graph-health tests.

Coverage:
    1. Roll-up counts match injected staleness map.
    2. Worst-first ordering (very_stale before stale before unknown before fresh).
    3. Missing lineage-quality.json -> graceful (freshness table still present,
       note about missing file in text output, graph: null in JSON output).
    4. --json emits valid JSON containing per-project states and summary.
    5. Non-vacuous: changing the injected map changes rendered counts.
    6. needs_sync = stale + very_stale only (unknown excluded).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load kb-status.py via importlib (hyphenated-style sibling; keep isolation).
# ---------------------------------------------------------------------------
_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_kb_status():
    spec = importlib.util.spec_from_file_location(
        "kb_status", _SKILL_DIR / "kb-status.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


KB_STATUS = _load_kb_status()

# ---------------------------------------------------------------------------
# Shared fixture: a staleness map spanning all four states.
# ---------------------------------------------------------------------------

MIXED_STALENESS: dict[str, dict] = {
    "alpha": {
        "state": "fresh",
        "head": "aaa",
        "documented_sha": "aaa",
        "drift_commits": 0,
        "drift_age_days": 0,
    },
    "bravo": {
        "state": "stale",
        "head": "bbb",
        "documented_sha": "b00",
        "drift_commits": 3,
        "drift_age_days": 5,
    },
    "charlie": {
        "state": "very_stale",
        "head": "ccc",
        "documented_sha": "c00",
        "drift_commits": 20,
        "drift_age_days": 45,
    },
    "delta": {
        "state": "unknown",
        "head": None,
        "documented_sha": None,
        "drift_commits": None,
        "drift_age_days": None,
    },
}


def _fake_lineage(tmp_path: Path, **overrides) -> Path:
    """Write a minimal lineage-quality.json to tmp_path/00-meta/ and return the vault root."""
    meta_dir = tmp_path / "00-meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "node_count": overrides.get("node_count", 100),
        "edge_count": overrides.get("edge_count", 200),
        "dangling_count": overrides.get("dangling_count", 5),
        "low_confidence_edge_count": overrides.get("low_confidence_edge_count", 30),
    }
    data = {
        "generated_at": "2026-06-19T00:00:00+00:00",
        "summary": summary,
    }
    (meta_dir / "lineage-quality.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Roll-up counts match injected staleness map
# ---------------------------------------------------------------------------

class TestRollUpCounts:
    def test_counts_match_injected_map(self, tmp_path):
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        summary = status["summary"]

        assert summary["total"] == 4
        assert summary["fresh"] == 1
        assert summary["stale"] == 1
        assert summary["very_stale"] == 1
        assert summary["unknown"] == 1

    def test_all_fresh(self, tmp_path):
        stale_map = {
            "p1": {"state": "fresh", "head": "x", "documented_sha": "x",
                   "drift_commits": 0, "drift_age_days": 0},
            "p2": {"state": "fresh", "head": "y", "documented_sha": "y",
                   "drift_commits": 0, "drift_age_days": 0},
        }
        status = KB_STATUS.build_status(tmp_path, staleness=stale_map)
        summary = status["summary"]

        assert summary["total"] == 2
        assert summary["fresh"] == 2
        assert summary["stale"] == 0
        assert summary["very_stale"] == 0
        assert summary["unknown"] == 0

    def test_empty_map(self, tmp_path):
        status = KB_STATUS.build_status(tmp_path, staleness={})
        summary = status["summary"]

        assert summary["total"] == 0
        assert summary["needs_sync"] == 0


# ---------------------------------------------------------------------------
# 2. Worst-first ordering
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_very_stale_before_fresh(self, tmp_path):
        stale_map = {
            "z-fresh": {"state": "fresh", "head": "f", "documented_sha": "f",
                        "drift_commits": 0, "drift_age_days": 0},
            "a-very-stale": {"state": "very_stale", "head": "v", "documented_sha": "v0",
                             "drift_commits": 50, "drift_age_days": 60},
        }
        status = KB_STATUS.build_status(tmp_path, staleness=stale_map)
        names = [p["name"] for p in status["projects"]]

        assert names.index("a-very-stale") < names.index("z-fresh"), (
            f"Expected very_stale before fresh but got order: {names}"
        )

    def test_full_ordering_worst_first(self, tmp_path):
        """very_stale < stale < unknown < fresh."""
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        names = [p["name"] for p in status["projects"]]

        # Derive positions by project
        pos = {p["name"]: i for i, p in enumerate(status["projects"])}

        # charlie=very_stale is worst → earliest
        assert pos["charlie"] < pos["bravo"], "very_stale must precede stale"
        assert pos["bravo"] < pos["delta"], "stale must precede unknown"
        assert pos["delta"] < pos["alpha"], "unknown must precede fresh"

    def test_same_state_sorted_by_name(self, tmp_path):
        """Projects in the same state are sorted alphabetically (deterministic)."""
        stale_map = {
            "zebra": {"state": "stale", "head": "z", "documented_sha": "z0",
                      "drift_commits": 1, "drift_age_days": 1},
            "apple": {"state": "stale", "head": "a", "documented_sha": "a0",
                      "drift_commits": 2, "drift_age_days": 2},
            "mango": {"state": "stale", "head": "m", "documented_sha": "m0",
                      "drift_commits": 3, "drift_age_days": 3},
        }
        status = KB_STATUS.build_status(tmp_path, staleness=stale_map)
        names = [p["name"] for p in status["projects"]]

        assert names == ["apple", "mango", "zebra"], (
            f"Expected alphabetical within same state, got {names}"
        )


# ---------------------------------------------------------------------------
# 3. Missing lineage-quality.json — graceful degradation
# ---------------------------------------------------------------------------

class TestMissingLineage:
    def test_missing_file_does_not_crash(self, tmp_path):
        """No lineage-quality.json present — build_status must not raise."""
        # tmp_path has no 00-meta/ directory
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)

        assert status["graph"] is None, (
            f"Expected graph=None when file missing, got {status['graph']!r}"
        )
        # Freshness data still present
        assert len(status["projects"]) == 4
        assert status["summary"]["total"] == 4

    def test_missing_file_text_render_notes_it(self, tmp_path):
        """render_text must mention the missing file and not crash."""
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        text = KB_STATUS.render_text(status)

        assert "lineage-quality.json" in text, (
            "Expected mention of lineage-quality.json when missing"
        )
        # Table header still present
        assert "PROJECT FRESHNESS" in text

    def test_missing_file_json_has_null_graph(self, tmp_path):
        """render_json must produce valid JSON with graph: null when file missing."""
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        raw = KB_STATUS.render_json(status)

        parsed = json.loads(raw)  # must not raise
        assert parsed["graph"] is None, (
            f"Expected graph=null in JSON when file missing, got {parsed['graph']!r}"
        )

    def test_freshness_table_present_without_lineage(self, tmp_path):
        """The freshness table must appear even when graph is missing."""
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        text = KB_STATUS.render_text(status)

        # All four projects must appear in the text output
        for name in MIXED_STALENESS:
            assert name in text, (
                f"Project '{name}' missing from text output when lineage absent"
            )


# ---------------------------------------------------------------------------
# 4. --json mode emits valid JSON with per-project states and summary
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_json_is_valid(self, tmp_path):
        _fake_lineage(tmp_path)
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        raw = KB_STATUS.render_json(status)

        parsed = json.loads(raw)  # must not raise
        assert "projects" in parsed
        assert "summary" in parsed
        assert "graph" in parsed

    def test_json_contains_per_project_states(self, tmp_path):
        _fake_lineage(tmp_path)
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        parsed = json.loads(KB_STATUS.render_json(status))

        states_by_name = {p["name"]: p["state"] for p in parsed["projects"]}
        assert states_by_name["alpha"] == "fresh"
        assert states_by_name["bravo"] == "stale"
        assert states_by_name["charlie"] == "very_stale"
        assert states_by_name["delta"] == "unknown"

    def test_json_summary_totals(self, tmp_path):
        _fake_lineage(tmp_path)
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        parsed = json.loads(KB_STATUS.render_json(status))

        summary = parsed["summary"]
        assert summary["total"] == 4
        assert summary["needs_sync"] == 2  # stale + very_stale

    def test_json_graph_health_populated(self, tmp_path):
        _fake_lineage(tmp_path, node_count=754, edge_count=1401,
                      dangling_count=53, low_confidence_edge_count=798)
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        parsed = json.loads(KB_STATUS.render_json(status))

        graph = parsed["graph"]
        assert graph is not None
        assert graph["node_count"] == 754
        assert graph["edge_count"] == 1401
        assert graph["dangling_count"] == 53
        assert graph["low_confidence_edge_count"] == 798


# ---------------------------------------------------------------------------
# 5. Non-vacuous: changing the injected map changes rendered counts
# ---------------------------------------------------------------------------

class TestNonVacuity:
    def test_count_changes_with_different_map(self, tmp_path):
        """Demonstrates the roll-up is actually reading the injected map.

        If the counts were hard-coded, both calls would return the same value.
        Non-vacuity proof: two different maps produce two different counts.
        """
        stale_map_a = {
            "p1": {"state": "stale", "head": "a", "documented_sha": "a0",
                   "drift_commits": 1, "drift_age_days": 1},
        }
        stale_map_b = {
            "p1": {"state": "fresh", "head": "b", "documented_sha": "b",
                   "drift_commits": 0, "drift_age_days": 0},
            "p2": {"state": "fresh", "head": "c", "documented_sha": "c",
                   "drift_commits": 0, "drift_age_days": 0},
        }

        status_a = KB_STATUS.build_status(tmp_path, staleness=stale_map_a)
        status_b = KB_STATUS.build_status(tmp_path, staleness=stale_map_b)

        assert status_a["summary"]["needs_sync"] == 1, (
            f"Map A should yield needs_sync=1, got {status_a['summary']['needs_sync']}"
        )
        assert status_b["summary"]["needs_sync"] == 0, (
            f"Map B should yield needs_sync=0, got {status_b['summary']['needs_sync']}"
        )
        assert status_a["summary"]["total"] != status_b["summary"]["total"], (
            "Different maps must produce different totals"
        )

    def test_text_render_changes_with_different_map(self, tmp_path):
        """Ensures render_text is reading the projects list, not a constant."""
        only_fresh = {
            "myproject": {"state": "fresh", "head": "x", "documented_sha": "x",
                          "drift_commits": 0, "drift_age_days": 0},
        }
        only_stale = {
            "myproject": {"state": "very_stale", "head": "y", "documented_sha": "y0",
                          "drift_commits": 99, "drift_age_days": 90},
        }

        text_fresh = KB_STATUS.render_text(
            KB_STATUS.build_status(tmp_path, staleness=only_fresh)
        )
        text_stale = KB_STATUS.render_text(
            KB_STATUS.build_status(tmp_path, staleness=only_stale)
        )

        assert "fresh" in text_fresh
        assert "very_stale" in text_stale
        assert text_fresh != text_stale, "Different states must produce different text"


# ---------------------------------------------------------------------------
# 6. needs_sync excludes unknown
# ---------------------------------------------------------------------------

class TestNeedsSync:
    def test_unknown_not_counted_in_needs_sync(self, tmp_path):
        stale_map = {
            "u1": {"state": "unknown", "head": None, "documented_sha": None,
                   "drift_commits": None, "drift_age_days": None},
            "u2": {"state": "unknown", "head": None, "documented_sha": None,
                   "drift_commits": None, "drift_age_days": None},
        }
        status = KB_STATUS.build_status(tmp_path, staleness=stale_map)
        assert status["summary"]["needs_sync"] == 0, (
            "unknown state must NOT count toward needs_sync"
        )
        assert status["summary"]["unknown"] == 2

    def test_needs_sync_is_stale_plus_very_stale(self, tmp_path):
        stale_map = {
            "a": {"state": "stale", "head": "a", "documented_sha": "a0",
                  "drift_commits": 1, "drift_age_days": 1},
            "b": {"state": "very_stale", "head": "b", "documented_sha": "b0",
                  "drift_commits": 10, "drift_age_days": 30},
            "c": {"state": "unknown", "head": None, "documented_sha": None,
                  "drift_commits": None, "drift_age_days": None},
            "d": {"state": "fresh", "head": "d", "documented_sha": "d",
                  "drift_commits": 0, "drift_age_days": 0},
        }
        status = KB_STATUS.build_status(tmp_path, staleness=stale_map)
        assert status["summary"]["needs_sync"] == 2, (
            f"needs_sync must be stale+very_stale only (2), got {status['summary']['needs_sync']}"
        )


# ---------------------------------------------------------------------------
# 7. Corrupt / unreadable lineage-quality.json — distinct from missing
# ---------------------------------------------------------------------------

class TestCorruptLineage:
    def _write_lineage(self, tmp_path: Path, content: bytes) -> None:
        meta_dir = tmp_path / "00-meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "lineage-quality.json").write_bytes(content)

    def test_malformed_json_returns_sentinel_not_none(self, tmp_path):
        """Malformed JSON must produce the unreadable sentinel, NOT None.

        Non-vacuity proof:
          - status["graph"] is not None  (distinguishes from the missing-file path)
          - status["graph"]["_error"] == "unreadable"  (proves _load_graph_health
            took the new except branch, not the missing-file early-return)
        """
        self._write_lineage(tmp_path, b"not json{")
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)

        assert status["graph"] is not None, (
            "Corrupt JSON must not return None (that's the missing-file sentinel)"
        )
        assert status["graph"].get("_error") == "unreadable", (
            f"Expected _error='unreadable', got {status['graph']!r}"
        )

    def test_malformed_json_render_text_shows_unreadable_not_not_found(self, tmp_path):
        """render_text must show 'unreadable', not 'not found', for corrupt JSON."""
        self._write_lineage(tmp_path, b"not json{")
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        text = KB_STATUS.render_text(status)

        assert "unreadable" in text, (
            "Expected 'unreadable' in render_text output for corrupt JSON"
        )
        assert "not found" not in text, (
            "render_text must NOT show 'not found' for a file that exists but is corrupt"
        )
        # Freshness table still intact
        assert "PROJECT FRESHNESS" in text

    def test_non_utf8_bytes_returns_sentinel_not_none(self, tmp_path):
        """Non-UTF-8 bytes must produce the unreadable sentinel, NOT None.

        Non-vacuity proof:
          - status["graph"] is not None  (distinguishes from the missing-file path)
          - status["graph"]["_error"] == "unreadable"  (proves UnicodeDecodeError
            was caught and the sentinel was returned, not a bare crash or None)
        """
        self._write_lineage(tmp_path, b"\xff\xfe bad")
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)

        assert status["graph"] is not None, (
            "Non-UTF-8 content must not return None (that's the missing-file sentinel)"
        )
        assert status["graph"].get("_error") == "unreadable", (
            f"Expected _error='unreadable', got {status['graph']!r}"
        )

    def test_non_utf8_bytes_render_text_shows_unreadable_not_not_found(self, tmp_path):
        """render_text must show 'unreadable', not 'not found', for non-UTF-8 bytes."""
        self._write_lineage(tmp_path, b"\xff\xfe bad")
        status = KB_STATUS.build_status(tmp_path, staleness=MIXED_STALENESS)
        text = KB_STATUS.render_text(status)

        assert "unreadable" in text, (
            "Expected 'unreadable' in render_text output for non-UTF-8 file"
        )
        assert "not found" not in text, (
            "render_text must NOT show 'not found' for a file that exists but has bad encoding"
        )
        # Freshness table still intact
        assert "PROJECT FRESHNESS" in text
