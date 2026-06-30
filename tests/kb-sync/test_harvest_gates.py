"""Tests for harvest_gates() in kb-harvest.py, and the extra_gates integration
in kb-graph.py build_graph_facts().

All tests are hermetic: no real vault, no real project repos.
Fixtures live under tests/fixtures/repo/{active/gates/, docs/}.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Importlib loaders (both modules have hyphenated filenames)
# ---------------------------------------------------------------------------

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_harvest():
    spec = importlib.util.spec_from_file_location(
        "kb_harvest", SKILL / "kb-harvest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_kb_graph():
    spec = importlib.util.spec_from_file_location(
        "kb_graph", SKILL / "kb-graph.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HARVEST = _load_harvest()
KB_GRAPH = _load_kb_graph()

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "repo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_note(path: str, title: str, note_type: str = "project", extra_fm: str = "") -> dict:
    fm_yaml = f"---\ntype: {note_type}\ntitle: \"{title}\"\n{extra_fm}---\n# {title}\n"
    fm = {"type": note_type, "title": title}
    return {"path": path, "fm": fm, "text": fm_yaml}


def _setup_db(notes: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE notes ("
        "path TEXT PRIMARY KEY, type TEXT, project TEXT, tool TEXT, "
        "\"group\" TEXT, title TEXT, status TEXT, severity TEXT, "
        "severity_rank INTEGER, stale INTEGER NOT NULL DEFAULT 0, last_seen_sha TEXT)"
    )
    for n in notes:
        conn.execute(
            "INSERT INTO notes (path, type, title) VALUES (?, ?, ?)",
            (n["path"], n["fm"].get("type"), n["fm"].get("title")),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests for harvest_gates() — artifact gate (active/gates/*.md)
# ---------------------------------------------------------------------------

class TestHarvestGatesArtifact:
    """Fixture repo has one artifact gate: active/gates/gate-001-security-review.md"""

    def test_artifact_gate_found(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        ids = [g["id"] for g in gates]
        assert "fix-pc1" in ids, f"Expected fix-pc1 in gates; got {ids}"

    def test_artifact_gate_status(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["status"] == "open"

    def test_artifact_gate_blocking(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["blocking"] is True

    def test_artifact_gate_title(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["title"] == "PC1 — Security review gate"

    def test_artifact_gate_gates_list(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["gates"] == ["prod-deploy"]

    def test_artifact_gate_requires_list(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["requires"] == ["external-audit"]

    def test_artifact_gate_criteria_count(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert len(g["criteria"]) == 3

    def test_artifact_gate_source_tag(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-pc1")
        assert g["source"] == "artifact"


# ---------------------------------------------------------------------------
# Tests for harvest_gates() — inline gate marker (docs/deploy-runbook.md)
# ---------------------------------------------------------------------------

class TestHarvestGatesInline:
    """Fixture docs/deploy-runbook.md has one inline gate: fix-g0."""

    def test_inline_gate_found(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        ids = [g["id"] for g in gates]
        assert "fix-g0" in ids, f"Expected fix-g0 in gates; got {ids}"

    def test_inline_gate_status(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["status"] == "open"

    def test_inline_gate_blocking(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["blocking"] is True

    def test_inline_gate_gates_list(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["gates"] == ["phase-1-live"]

    def test_inline_gate_requires_list(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["requires"] == ["projgamma"]

    def test_inline_gate_source_tag(self):
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["source"] == "inline"

    def test_inline_gate_title_is_none(self):
        """Inline gates have no title (only artifact gates have one)."""
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        g = next(x for x in gates if x["id"] == "fix-g0")
        assert g["title"] is None


# ---------------------------------------------------------------------------
# Tests for harvest_gates() — global guarantees
# ---------------------------------------------------------------------------

class TestHarvestGatesGlobal:
    """Sorting, dedup, empty-repo safety."""

    def test_sorted_by_id(self):
        """Output list is sorted by gate-id for determinism."""
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        ids = [g["id"] for g in gates]
        assert ids == sorted(ids), f"Gates not sorted by id: {ids}"

    def test_no_duplicates(self):
        """Each gate-id appears at most once."""
        gates = HARVEST.harvest_gates(FIXTURE_REPO)
        ids = [g["id"] for g in gates]
        assert len(ids) == len(set(ids)), f"Duplicate gate-ids found: {ids}"

    def test_empty_repo_returns_empty(self, tmp_path):
        """A repo with no active/ or docs/ dirs → empty list (no error)."""
        result = HARVEST.harvest_gates(tmp_path)
        assert result == []

    def test_missing_gate_id_skipped(self, tmp_path):
        """An artifact gate file missing gate-id is silently skipped."""
        gates_dir = tmp_path / "active" / "gates"
        gates_dir.mkdir(parents=True)
        (gates_dir / "bad-gate.md").write_text(
            "---\ntype: gate\ntitle: \"Missing ID Gate\"\nstatus: open\n---\n",
            encoding="utf-8",
        )
        result = HARVEST.harvest_gates(tmp_path)
        assert result == []

    def test_non_gate_type_skipped(self, tmp_path):
        """active/gates/*.md file with type: project (not gate) is not harvested."""
        gates_dir = tmp_path / "active" / "gates"
        gates_dir.mkdir(parents=True)
        (gates_dir / "not-a-gate.md").write_text(
            "---\ntype: project\ntitle: \"Some Project\"\ngate-id: \"proj-1\"\n---\n",
            encoding="utf-8",
        )
        result = HARVEST.harvest_gates(tmp_path)
        assert result == []

    def test_planning_dir_skipped(self, tmp_path):
        """Files under .planning/ must not be scanned (GSD-owned)."""
        # Place an inline gate inside .planning — it must be ignored.
        planning_docs = tmp_path / ".planning" / "docs"
        planning_docs.mkdir(parents=True)
        (planning_docs / "plan.md").write_text(
            "<!-- @gate id=skip-me status=open blocking=true gates=x requires=[] -->\n",
            encoding="utf-8",
        )
        result = HARVEST.harvest_gates(tmp_path)
        ids = [g["id"] for g in result]
        assert "skip-me" not in ids, f".planning gate leaked into results: {ids}"

    def test_active_dir_not_gates_subdir_scanned(self, tmp_path):
        """Inline markers in active/ (non-gates subdir) are still found."""
        active_dir = tmp_path / "active" / "plans"
        active_dir.mkdir(parents=True)
        (active_dir / "roadmap.md").write_text(
            "<!-- @gate id=active-inline status=open blocking=false gates=[] requires=[] -->\n",
            encoding="utf-8",
        )
        result = HARVEST.harvest_gates(tmp_path)
        ids = [g["id"] for g in result]
        assert "active-inline" in ids, f"inline gate in active/plans/ not found: {ids}"


# ---------------------------------------------------------------------------
# Tests for harvest_structured() gates integration
# ---------------------------------------------------------------------------

class TestHarvestStructuredGates:
    """harvest_structured() must include the gates key and count."""

    def test_gates_key_present(self):
        result = HARVEST.harvest_structured(FIXTURE_REPO, "fixture", "1.0-dev", "abc123")
        assert "gates" in result, "harvest_structured() must return a 'gates' key"

    def test_gates_count_in_harvest_counts(self):
        result = HARVEST.harvest_structured(FIXTURE_REPO, "fixture", "1.0-dev", "abc123")
        assert "gates" in result["harvest_counts"], (
            "harvest_counts must include a 'gates' entry"
        )
        assert result["harvest_counts"]["gates"] == len(result["gates"])

    def test_gates_is_in_structured_keys(self):
        """'gates' must be listed in _STRUCTURED_KEYS so it's overwritten each run."""
        assert "gates" in HARVEST._STRUCTURED_KEYS


# ---------------------------------------------------------------------------
# Tests for build_graph_facts() extra_gates integration
# ---------------------------------------------------------------------------

class TestBuildGraphFactsExtraGates:
    """extra_gates parameter must inject repo gates into graph as synthetic nodes."""

    def _run(self, tmp_path, extra_gates=None):
        notes = [_make_note("folder/proj-a.md", "Project A")]
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path, extra_gates)
        return conn, stats

    def test_extra_gate_node_in_graph_json(self, tmp_path):
        """An extra_gate must appear as a node in graph.json."""
        extra = [
            {
                "id": "eg-gate1",
                "status": "open",
                "blocking": True,
                "gates": [],
                "requires": [],
                "criteria": ["Check one", "Check two"],
                "source": "artifact",
                "title": "EG Gate 1",
                "ref": None,
            }
        ]
        self._run(tmp_path, extra_gates=extra)
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        ids = [n["id"] for n in graph["nodes"]]
        assert "eg-gate1" in ids, f"eg-gate1 not found in graph nodes: {ids}"

    def test_extra_gate_label_from_title(self, tmp_path):
        """Artifact gate extra_gates use title as label (not gate-id)."""
        extra = [
            {
                "id": "eg-gate2",
                "status": "open",
                "blocking": False,
                "gates": [],
                "requires": [],
                "criteria": [],
                "source": "artifact",
                "title": "Named Gate Two",
                "ref": None,
            }
        ]
        self._run(tmp_path, extra_gates=extra)
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        node = next((n for n in graph["nodes"] if n["id"] == "eg-gate2"), None)
        assert node is not None
        assert node["label"] == "Named Gate Two", (
            f"Expected 'Named Gate Two' as label, got {node.get('label')!r}"
        )

    def test_extra_gate_inline_label_fallback(self, tmp_path):
        """Inline extra_gate with no title uses gate-id as label."""
        extra = [
            {
                "id": "eg-gate3",
                "status": "open",
                "blocking": True,
                "gates": [],
                "requires": [],
                "criteria": [],
                "source": "inline",
                "title": None,
                "ref": None,
            }
        ]
        self._run(tmp_path, extra_gates=extra)
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        node = next((n for n in graph["nodes"] if n["id"] == "eg-gate3"), None)
        assert node is not None
        assert node["label"] == "eg-gate3", (
            f"Expected gate-id as label fallback, got {node.get('label')!r}"
        )

    def test_extra_gate_criteria_count(self, tmp_path):
        extra = [
            {
                "id": "eg-gate4",
                "status": "open",
                "blocking": True,
                "gates": [],
                "requires": [],
                "criteria": ["A", "B", "C"],
                "source": "artifact",
                "title": None,
                "ref": None,
            }
        ]
        self._run(tmp_path, extra_gates=extra)
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        node = next((n for n in graph["nodes"] if n["id"] == "eg-gate4"), None)
        assert node is not None
        assert node["criteria_count"] == 3

    def test_extra_gate_gate_count_in_stats(self, tmp_path):
        """Stats gate_count must increase by 1 for each extra_gate added."""
        extra = [
            {
                "id": "eg-gate5",
                "status": "open",
                "blocking": True,
                "gates": [],
                "requires": [],
                "criteria": [],
                "source": "inline",
                "title": None,
                "ref": None,
            }
        ]
        _, stats = self._run(tmp_path, extra_gates=extra)
        assert stats["gate_count"] >= 1, (
            f"Expected gate_count ≥ 1, got {stats['gate_count']}"
        )

    def test_none_extra_gates_is_safe(self, tmp_path):
        """Passing extra_gates=None must not raise; gate_count stays 0."""
        _, stats = self._run(tmp_path, extra_gates=None)
        assert stats["gate_count"] == 0

    def test_extra_gate_dedup_vault_wins(self, tmp_path):
        """If a vault inline gate and an extra_gate share the same id, vault wins."""
        # Create a vault note with an inline gate
        notes = [
            _make_note("folder/proj-a.md", "Project A"),
            {
                "path": "folder/note-with-gate.md",
                "fm": {"type": "project", "title": "Note With Gate"},
                "text": (
                    "---\ntype: project\ntitle: \"Note With Gate\"\n---\n"
                    "<!-- @gate id=shared-gate status=closed blocking=false "
                    "gates=[] requires=[] -->\n"
                ),
            },
        ]
        extra = [
            {
                "id": "shared-gate",
                "status": "open",  # repo says open — vault says closed; vault wins
                "blocking": True,
                "gates": [],
                "requires": [],
                "criteria": [],
                "source": "inline",
                "title": None,
                "ref": None,
            }
        ]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path, extra)
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        node = next((n for n in graph["nodes"] if n["id"] == "shared-gate"), None)
        assert node is not None
        # Vault gate has status=closed; extra_gate has status=open
        assert node["status"] == "closed", (
            f"Vault gate (status=closed) should win over repo gate; got {node['status']!r}"
        )

    def test_extra_gate_dangling_edge_counted(self, tmp_path):
        """A gate with gates=['unknown-target'] → dangling edge counted, gate node kept."""
        extra = [
            {
                "id": "gate-with-dangling",
                "status": "open",
                "blocking": True,
                "gates": ["unknown-target-that-does-not-exist"],
                "requires": [],
                "criteria": [],
                "source": "inline",
                "title": None,
                "ref": None,
            }
        ]
        _, stats = self._run(tmp_path, extra_gates=extra)
        assert stats["dangling_dropped"] >= 1, (
            f"Expected ≥1 dangling from the unresolved gates= target; "
            f"got dangling={stats['dangling_dropped']}"
        )
        # Gate node itself must still be in graph.json
        graph = json.loads((tmp_path / "00-meta" / "graph.json").read_text())
        ids = [n["id"] for n in graph["nodes"]]
        assert "gate-with-dangling" in ids, (
            f"gate-with-dangling must be kept even when its edges are dangling; got {ids}"
        )
