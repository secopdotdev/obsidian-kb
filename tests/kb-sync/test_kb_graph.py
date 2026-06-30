"""Tests for kb-graph.py — deterministic graph-facts pass.

All tests are hermetic (in-memory sqlite3 + tmp_path; no real vault dependency).

Fixture topology:
    Nodes: note-a, note-b, note-c (3-cycle: A requires B, B requires C, C requires A)
           note-d, note-e (acyclic supersession chain: D → E)
           note-orphan    (no edges; isolated node)

    Edges extracted:
      A requires B  →  edge B→A  "requires"   (B is prereq of A)
      B requires C  →  edge C→B  "requires"
      C requires A  →  edge A→C  "requires"   ← this closes the cycle
      D supersedes E → edge E→D  "supersedes" (older E → newer D)
      Also note-d has up: note-e → edge note-e→note-d "partof"

Edge direction recap (upstream→downstream):
  requires: [X]   →  X → N  "requires"
  up: X           →  X → N  "partof"
  supersedes: X   →  X → N  "supersedes"
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import kb-graph.py (hyphenated name — must use importlib).
# ---------------------------------------------------------------------------
SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_kb_graph():
    spec = importlib.util.spec_from_file_location(
        "kb_graph", SKILL / "kb-graph.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KB_GRAPH = _load_kb_graph()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_note(path: str, title: str, note_type: str = "project", extra_fm: str = "") -> dict:
    """Build a note dict (path + fm + text) as kb-index would produce it."""
    fm_yaml = f"---\ntype: {note_type}\ntitle: \"{title}\"\n{extra_fm}---\n# {title}\n"
    fm = {"type": note_type, "title": title}
    return {"path": path, "fm": fm, "text": fm_yaml}


def _setup_db(notes: list[dict]) -> sqlite3.Connection:
    """Create an in-memory DB with a `notes` table populated from *notes*."""
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
# Fixture: 3-node cycle + acyclic chain + orphan
# ---------------------------------------------------------------------------

def _build_fixture_notes() -> list[dict]:
    """
    note-a: requires: ["[[note-b]]"]                      → edge note-b→note-a "requires"
    note-b: requires: ["[[note-c]]"]                      → edge note-c→note-b "requires"
    note-c: requires: ["[[note-a]]"]                      → edge note-a→note-c "requires"
    note-d: supersedes: "[[note-e]]"                      → edge note-e→note-d "supersedes"
            up: "[[note-e]]"                              → edge note-e→note-d "partof"
    note-e: (no outgoing edges; sits upstream of note-d)
    note-orphan: (no edges at all)
    dangling: note-a also references [[note-missing]] in related → 1 dangling
    """
    notes = [
        _make_note(
            "folder/note-a.md", "note-a",
            extra_fm=(
                'requires: ["[[note-b]]"]\n'
                'related: ["[[note-missing]]"]\n'
            ),
        ),
        _make_note(
            "folder/note-b.md", "note-b",
            extra_fm='requires: ["[[note-c]]"]\n',
        ),
        _make_note(
            "folder/note-c.md", "note-c",
            extra_fm='requires: ["[[note-a]]"]\n',
        ),
        _make_note(
            "folder/note-d.md", "note-d",
            extra_fm=(
                'supersedes: "[[note-e]]"\n'
                'up: "[[note-e]]"\n'
            ),
        ),
        _make_note("folder/note-e.md", "note-e"),
        _make_note("folder/note-orphan.md", "note-orphan"),
    ]
    return notes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCycleDetection:
    """3-node cycle A→B→C→A must be detected and assigned a shared cycle_id."""

    def _run(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats

    def test_cycle_detected(self, tmp_path):
        conn, stats = self._run(tmp_path)
        assert stats["cycle_count"] == 1, (
            f"expected 1 cycle (the 3-node A-B-C cycle), got {stats['cycle_count']}"
        )

    def test_all_three_share_cycle_id(self, tmp_path):
        conn, stats = self._run(tmp_path)
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts WHERE slug IN "
            "('folder/note-a.md','folder/note-b.md','folder/note-c.md') "
            "ORDER BY slug"
        ).fetchall()
        assert len(rows) == 3
        cids = {r[1] for r in rows}
        assert None not in cids, f"cycle_id should not be NULL for cycle members; got {rows}"
        assert len(cids) == 1, (
            f"all three cycle members must share one cycle_id; got ids={cids} rows={rows}"
        )

    def test_exactly_one_break_edge(self, tmp_path):
        """A 3-node cycle has exactly one DFS back-edge → exactly one break_edge in the table."""
        conn, stats = self._run(tmp_path)
        break_edges = conn.execute(
            "SELECT src, dst, type FROM edges WHERE break_edge = 1"
        ).fetchall()
        assert len(break_edges) == 1, (
            f"expected exactly 1 break edge for the 3-node cycle, got {break_edges}"
        )

    def test_break_edge_is_within_cycle(self, tmp_path):
        conn, stats = self._run(tmp_path)
        be = conn.execute(
            "SELECT src, dst FROM edges WHERE break_edge = 1"
        ).fetchone()
        assert be is not None
        cycle_nodes = {"folder/note-a.md", "folder/note-b.md", "folder/note-c.md"}
        assert be[0] in cycle_nodes and be[1] in cycle_nodes, (
            f"break edge {be} should connect two cycle nodes"
        )


class TestAcyclicPart:
    """The supersession chain note-e → note-d must be acyclic and rank-monotonic."""

    def _run(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats

    def test_acyclic_nodes_have_no_cycle_id(self, tmp_path):
        conn, stats = self._run(tmp_path)
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts "
            "WHERE slug IN ('folder/note-d.md','folder/note-e.md','folder/note-orphan.md')"
        ).fetchall()
        for slug, cid in rows:
            assert cid is None, f"acyclic node {slug!r} should have NULL cycle_id, got {cid}"

    def test_topo_rank_monotonic_on_chain(self, tmp_path):
        """note-e is the upstream (no prereqs); note-d depends on note-e → rank(d) > rank(e)."""
        conn, stats = self._run(tmp_path)
        rank_e = conn.execute(
            "SELECT topo_rank FROM graph_facts WHERE slug = 'folder/note-e.md'"
        ).fetchone()[0]
        rank_d = conn.execute(
            "SELECT topo_rank FROM graph_facts WHERE slug = 'folder/note-d.md'"
        ).fetchone()[0]
        assert rank_d > rank_e, (
            f"note-d (downstream) should have higher rank than note-e (upstream); "
            f"rank_e={rank_e}, rank_d={rank_d}"
        )

    def test_orphan_rank_zero(self, tmp_path):
        conn, stats = self._run(tmp_path)
        row = conn.execute(
            "SELECT topo_rank FROM graph_facts WHERE slug = 'folder/note-orphan.md'"
        ).fetchone()
        assert row is not None
        assert row[0] == 0, f"isolated node should have rank 0, got {row[0]}"


class TestDanglingEdges:
    """Edges to missing nodes must be dropped, not inserted."""

    def test_dangling_counted_not_inserted(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        # note-a has related: ["[[note-missing]]"] → 1 dangling
        assert stats["dangling_dropped"] >= 1, (
            f"expected ≥1 dangling (the [[note-missing]] ref), got {stats['dangling_dropped']}"
        )

    def test_dangling_not_in_edges_table(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        # No edge should reference a non-existent node path.
        node_paths = {
            r[0] for r in conn.execute("SELECT path FROM notes").fetchall()
        }
        all_edges = conn.execute("SELECT src, dst FROM edges").fetchall()
        for src, dst in all_edges:
            assert src in node_paths, f"dangling src in edges table: {src}"
            assert dst in node_paths, f"dangling dst in edges table: {dst}"


class TestGraphJson:
    """graph.json must be emitted with correct shape and content."""

    def test_graph_json_emitted(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        gj = tmp_path / "00-meta" / "graph.json"
        assert gj.exists(), "graph.json was not created"

    def test_graph_json_schema(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        assert "nodes" in data and "edges" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)

    def test_node_count(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["nodes"]) == len(notes), (
            f"expected {len(notes)} nodes in graph.json, got {len(data['nodes'])}"
        )

    def test_node_required_fields(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        required = {"id", "label", "type", "rag", "group", "topo_rank", "cycle_id", "goal", "next", "blocker"}
        for node in data["nodes"]:
            missing = required - set(node.keys())
            assert not missing, f"node {node.get('id')!r} missing fields: {missing}"

    def test_edge_required_fields(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        required = {"s", "d", "t", "cycle", "brk"}
        for edge in data["edges"]:
            missing = required - set(edge.keys())
            assert not missing, f"edge {edge} missing fields: {missing}"

    def test_cycle_edges_marked_in_json(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        cycle_edges = [e for e in data["edges"] if e["cycle"]]
        assert len(cycle_edges) > 0, "expected some edges marked cycle=true for the 3-node cycle"

    def test_break_edge_marked_in_json(self, tmp_path):
        """Exactly one canonical brk:true per SCC (3-cycle → 1 break edge in JSON)."""
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        break_edges = [e for e in data["edges"] if e["brk"]]
        assert len(break_edges) == 1, (
            f"expected exactly 1 canonical break edge in graph.json, got {break_edges}"
        )

    def test_goal_field_is_bool(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        for node in data["nodes"]:
            assert isinstance(node["goal"], bool), (
                f"goal should be a bool, got {type(node['goal'])} for {node['id']!r}"
            )


class TestIdempotency:
    """Running build_graph_facts twice must produce identical results."""

    def test_double_run_idempotent(self, tmp_path):
        notes = _build_fixture_notes()

        conn1 = _setup_db(notes)
        s1 = KB_GRAPH.build_graph_facts(conn1, notes, tmp_path)
        edges1 = sorted(conn1.execute("SELECT src, dst, type, cycle, break_edge FROM edges").fetchall())
        facts1 = sorted(conn1.execute("SELECT slug, cycle_id, topo_rank FROM graph_facts").fetchall())
        conn1.close()

        conn2 = _setup_db(notes)
        s2 = KB_GRAPH.build_graph_facts(conn2, notes, tmp_path)
        edges2 = sorted(conn2.execute("SELECT src, dst, type, cycle, break_edge FROM edges").fetchall())
        facts2 = sorted(conn2.execute("SELECT slug, cycle_id, topo_rank FROM graph_facts").fetchall())
        conn2.close()

        assert edges1 == edges2, "edges table not idempotent"
        assert facts1 == facts2, "graph_facts table not idempotent"
        assert s1 == s2, "stats not idempotent"


class TestEdgeTypes:
    """Verify specific edge types are recorded correctly."""

    def _run(self, tmp_path):
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats

    def test_requires_edges_present(self, tmp_path):
        conn, _ = self._run(tmp_path)
        reqs = conn.execute(
            "SELECT src, dst FROM edges WHERE type = 'requires'"
        ).fetchall()
        assert len(reqs) == 3, f"expected 3 'requires' edges (cycle), got {reqs}"

    def test_supersedes_edge_present(self, tmp_path):
        conn, _ = self._run(tmp_path)
        supers = conn.execute(
            "SELECT src, dst FROM edges WHERE type = 'supersedes'"
        ).fetchall()
        # note-e → note-d (older note-e superseded by newer note-d)
        assert ("folder/note-e.md", "folder/note-d.md") in supers, (
            f"expected note-e→note-d supersedes edge; got {supers}"
        )

    def test_partof_edge_present(self, tmp_path):
        conn, _ = self._run(tmp_path)
        partof = conn.execute(
            "SELECT src, dst FROM edges WHERE type = 'partof'"
        ).fetchall()
        assert ("folder/note-e.md", "folder/note-d.md") in partof, (
            f"expected note-e→note-d partof edge; got {partof}"
        )


class TestWikilinkParsing:
    """Verify wikilink resolution: block list, inline list, scalar wikilinks."""

    def test_inline_list_related(self, tmp_path):
        """related: inline list → correct edges inserted (excluding dangling)."""
        note_a = _make_note(
            "a.md", "note-a",
            extra_fm='related: ["[[note-b]]"]\n',
        )
        note_b = _make_note("b.md", "note-b")
        notes = [note_a, note_b]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        edges = conn.execute("SELECT src, dst, type FROM edges").fetchall()
        assert ("a.md", "b.md", "related") in edges, (
            f"expected a→b related edge; got {edges}"
        )

    def test_scalar_supersedes_comma_separated(self, tmp_path):
        """supersedes: '[[note-x]], [[note-y]]' → two supersedes edges."""
        note_a = _make_note(
            "new.md", "new",
            extra_fm='supersedes: "[[old-x]], [[old-y]]"\n',
        )
        note_x = _make_note("old-x.md", "old-x")
        note_y = _make_note("old-y.md", "old-y")
        notes = [note_a, note_x, note_y]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        edges = conn.execute("SELECT src, dst, type FROM edges WHERE type='supersedes'").fetchall()
        assert len(edges) == 2, f"expected 2 supersedes edges; got {edges}"
        srcs = {e[0] for e in edges}
        assert srcs == {"old-x.md", "old-y.md"}, f"expected old-x and old-y as srcs; got {srcs}"

    def test_superseded_by_normalized(self, tmp_path):
        """superseded-by: X → edge self→X (older→newer = self→X)."""
        note_old = _make_note(
            "old.md", "old",
            extra_fm='superseded-by: "[[new]]"\n',
        )
        note_new = _make_note("new.md", "new")
        notes = [note_old, note_new]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        edges = conn.execute("SELECT src, dst, type FROM edges WHERE type='supersedes'").fetchall()
        assert ("old.md", "new.md", "supersedes") in edges, (
            f"expected old→new supersedes edge; got {edges}"
        )

    def test_blockers_dict_items_not_edges(self, tmp_path):
        """blockers: block list of dicts must NOT produce edges or dangling counts."""
        note = _make_note(
            "proj.md", "proj",
            extra_fm=(
                "blockers:\n"
                "  - text: \"Some blocker text\"\n"
                "    severity: high\n"
                "    since: \"2026-01-01\"\n"
            ),
        )
        notes = [note]
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        edges = conn.execute("SELECT * FROM edges").fetchall()
        assert edges == [], f"dict-type blockers should not produce edges; got {edges}"
        assert stats["dangling_dropped"] == 0, (
            f"dict-type blockers should not produce dangling count; got {stats['dangling_dropped']}"
        )


# ---------------------------------------------------------------------------
# NEW: Fix 1 — related edges must never influence cycle detection
# ---------------------------------------------------------------------------

class TestRelatedEdgesExcludedFromCycles:
    """related edges are associative and must never participate in cycle detection.

    Fixture: a genuine 3-node prerequisite cycle (A→B→C→A via 'requires') PLUS
    related edges that form their own would-be cycle among nodes that have NO
    prerequisite relationship (X→Y→Z→X via 'related').  The related-only nodes
    must receive NULL cycle_id.  The prerequisite cycle must still be detected.

    Additionally, a related edge between two members of the prereq cycle must be
    stored with cycle=0 and brk=False (never force-flagged by their membership).
    """

    def _build_notes(self) -> list[dict]:
        # Prerequisite 3-cycle: req-a → req-b → req-c → req-a
        # Edge direction: requires [X] → edge X→note
        # req-a requires req-b → edge req-b → req-a
        # req-b requires req-c → edge req-c → req-b
        # req-c requires req-a → edge req-a → req-c  (closes prereq cycle)
        notes = [
            _make_note(
                "cycle/req-a.md", "req-a",
                extra_fm='requires: ["[[req-b]]"]\n',
            ),
            _make_note(
                "cycle/req-b.md", "req-b",
                extra_fm=(
                    'requires: ["[[req-c]]"]\n'
                    # related edge between two prereq-cycle members: must not affect cycle/brk
                    'related: ["[[req-a]]"]\n'
                ),
            ),
            _make_note(
                "cycle/req-c.md", "req-c",
                extra_fm='requires: ["[[req-a]]"]\n',
            ),
            # related-only nodes: would form a cycle X→Y→Z→X if related were counted
            _make_note(
                "rel/rel-x.md", "rel-x",
                extra_fm='related: ["[[rel-y]]"]\n',
            ),
            _make_note(
                "rel/rel-y.md", "rel-y",
                extra_fm='related: ["[[rel-z]]"]\n',
            ),
            _make_note(
                "rel/rel-z.md", "rel-z",
                extra_fm='related: ["[[rel-x]]"]\n',
            ),
        ]
        return notes

    def test_prereq_cycle_detected(self, tmp_path):
        notes = self._build_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["cycle_count"] == 1, (
            f"expected exactly 1 prereq cycle; got {stats['cycle_count']}"
        )

    def test_related_only_nodes_have_no_cycle_id(self, tmp_path):
        """rel-x, rel-y, rel-z are connected only via related edges → NULL cycle_id."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts "
            "WHERE slug IN ('rel/rel-x.md','rel/rel-y.md','rel/rel-z.md')"
        ).fetchall()
        assert len(rows) == 3, f"expected 3 related-only nodes in graph_facts; got {rows}"
        for slug, cid in rows:
            assert cid is None, (
                f"related-only node {slug!r} must have NULL cycle_id (got {cid}); "
                "related edges must not participate in cycle detection"
            )

    def test_related_edge_between_cycle_members_has_cycle_zero(self, tmp_path):
        """The related edge req-b→req-a must be stored with cycle=0 even though both
        endpoints are in the prerequisite cycle.  Related edges are never cycle-flagged."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        row = conn.execute(
            "SELECT cycle, break_edge FROM edges "
            "WHERE src = 'cycle/req-b.md' AND dst = 'cycle/req-a.md' AND type = 'related'"
        ).fetchone()
        assert row is not None, "expected a related edge req-b→req-a in the edges table"
        cycle_val, brk_val = row
        assert cycle_val == 0, (
            f"related edge between cycle members must have cycle=0, got cycle={cycle_val}"
        )
        assert brk_val == 0, (
            f"related edge between cycle members must have break_edge=0, got break_edge={brk_val}"
        )

    def test_related_edge_brk_false_in_json(self, tmp_path):
        """The related edge req-b→req-a must have brk:false in graph.json."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        related_edges = [
            e for e in data["edges"]
            if e["t"] == "related"
            and e["s"] == "cycle/req-b.md"
            and e["d"] == "cycle/req-a.md"
        ]
        assert len(related_edges) == 1, (
            f"expected exactly one related edge req-b→req-a in JSON; got {related_edges}"
        )
        assert related_edges[0]["brk"] is False, (
            f"related edge must have brk:false; got {related_edges[0]}"
        )
        assert related_edges[0]["cycle"] is False, (
            f"related edge must have cycle:false; got {related_edges[0]}"
        )


# ---------------------------------------------------------------------------
# NEW: Fix 2 — 4-node SCC with 2 back-edges gets real topo_rank
# ---------------------------------------------------------------------------

class TestLargeSCCBreakEdges:
    """A 4-node SCC containing 2 DFS back-edges must:
    - have ALL members get a non-zero topo_rank spread after back-edge removal
    - have break_edge=1 for MORE than one edge in the SQLite table
    - have brk:true for exactly ONE canonical edge in graph.json

    Topology (prereq_adj — all 'requires' type):
        p → q, q → r, r → p   (back-edge: r→p detected first by DFS)
        r → s, s → q           (back-edge: s→q)

    Edge encoding via frontmatter 'requires':
        note-q: requires [note-p, note-s]   → edges p→q and s→q
        note-r: requires [note-q]            → edge q→r
        note-p: requires [note-r]            → edge r→p
        note-s: requires [note-r]            → edge r→s

    prereq_adj:
        p → [q]        (because note-q requires note-p → p is prereq of q → p→q)
        q → [r]
        r → [p, s]
        s → [q]

    DFS from p (lex-sorted: p < q < r < s):
        grey(p) → grey(q) → grey(r) → grey(p is grey): back-edge(r,p)
                                     → grey(s) → grey(q is grey): back-edge(s,q)
                                       s→black, r→black, q→black, p→black

    Residual DAG after removing {(r,p),(s,q)}: p→q, q→r, r→s
    Topo ranks: p=0, q=1, r=2, s=3  → all distinct, max=3
    """

    def _build_notes(self) -> list[dict]:
        return [
            _make_note(
                "scc/note-p.md", "note-p",
                # r is prereq of p → edge r→p
                extra_fm='requires: ["[[note-r]]"]\n',
            ),
            _make_note(
                "scc/note-q.md", "note-q",
                # p and s are prereqs of q → edges p→q and s→q
                extra_fm='requires: ["[[note-p]]", "[[note-s]]"]\n',
            ),
            _make_note(
                "scc/note-r.md", "note-r",
                # q is prereq of r → edge q→r
                extra_fm='requires: ["[[note-q]]"]\n',
            ),
            _make_note(
                "scc/note-s.md", "note-s",
                # r is prereq of s → edge r→s
                extra_fm='requires: ["[[note-r]]"]\n',
            ),
        ]

    def _run(self, tmp_path):
        notes = self._build_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats, notes

    def test_single_scc_detected(self, tmp_path):
        conn, stats, _ = self._run(tmp_path)
        assert stats["cycle_count"] == 1, (
            f"expected 1 SCC for the 4-node cycle; got {stats['cycle_count']}"
        )

    def test_all_four_share_cycle_id(self, tmp_path):
        conn, stats, _ = self._run(tmp_path)
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts ORDER BY slug"
        ).fetchall()
        cids = {r[1] for r in rows}
        assert None not in cids, f"all 4 SCC members should have a cycle_id; got {rows}"
        assert len(cids) == 1, f"all 4 members should share one cycle_id; got {cids}"

    def test_multiple_break_edges_in_table(self, tmp_path):
        """After full DFS back-edge detection, the 4-node SCC must have >1 break_edge=1 row."""
        conn, stats, _ = self._run(tmp_path)
        break_edges = conn.execute(
            "SELECT src, dst FROM edges WHERE break_edge = 1"
        ).fetchall()
        assert len(break_edges) > 1, (
            f"expected >1 break_edge=1 rows for the 4-node SCC with 2 back-edges; "
            f"got {break_edges}"
        )

    def test_exactly_one_canonical_brk_in_json(self, tmp_path):
        """graph.json must have exactly one brk:true edge (canonical per SCC)."""
        conn, stats, notes = self._run(tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        brk_edges = [e for e in data["edges"] if e["brk"]]
        assert len(brk_edges) == 1, (
            f"expected exactly 1 canonical brk:true edge in graph.json; got {brk_edges}"
        )

    def test_topo_ranks_non_degenerate(self, tmp_path):
        """All 4 SCC members must have a topo_rank spread of ≥1 after back-edge removal.

        A fully degenerate result (all rank 0) means cycles were not properly broken.
        """
        conn, stats, _ = self._run(tmp_path)
        rows = conn.execute(
            "SELECT slug, topo_rank FROM graph_facts ORDER BY slug"
        ).fetchall()
        ranks = [r[1] for r in rows]
        max_rank = max(ranks)
        assert max_rank >= 1, (
            f"expected max topo_rank ≥ 1 for the 4-node SCC after back-edge removal; "
            f"all ranks={dict(rows)}"
        )
        # Verify no node is stuck at rank 0 when it should have propagated.
        # The residual DAG p→q→r→s gives ranks 0,1,2,3 — min should be 0 (root p),
        # but max must be at least 1 (q has an incoming edge from p).
        rank_dict = {r[0]: r[1] for r in rows}
        assert rank_dict.get("scc/note-p.md", -1) == 0, (
            f"note-p is the DAG root (no prereqs in residual DAG); expected rank 0"
        )
        assert rank_dict.get("scc/note-q.md", -1) >= 1, (
            f"note-q depends on note-p in the residual DAG; expected rank ≥ 1"
        )


# ---------------------------------------------------------------------------
# NEW: Gate recognition tests
# ---------------------------------------------------------------------------

def _make_note_with_text(path: str, title: str, note_type: str, full_text: str) -> dict:
    """Build a note dict with fully custom text (for inline gate tests)."""
    fm = {"type": note_type, "title": title}
    return {"path": path, "fm": fm, "text": full_text}


class TestInlineGateParsing:
    """_parse_inline_gates: attr extraction + criteria capture."""

    def test_inline_gate_basic_attrs(self):
        """All named attrs are extracted from a well-formed inline marker."""
        text = (
            "# Note\n"
            "<!-- @gate id=avv-g0 status=open blocking=true gates=phase-21-live "
            "requires=[projgamma-power,projgamma-creds] -->\n"
        )
        gates, skipped = KB_GRAPH._parse_inline_gates(text)
        assert skipped == 0
        assert len(gates) == 1
        g = gates[0]
        assert g["id"] == "avv-g0"
        assert g["status"] == "open"
        assert g["blocking"] is True
        assert g["gates"] == ["phase-21-live"]
        assert sorted(g["requires"]) == ["projgamma-creds", "projgamma-power"]

    def test_bracket_requires_form(self):
        """requires=[a,b] bracket form parses to a two-element list."""
        text = "<!-- @gate id=g1 requires=[alpha,beta] -->\n"
        gates, _ = KB_GRAPH._parse_inline_gates(text)
        assert sorted(gates[0]["requires"]) == ["alpha", "beta"]

    def test_comma_gates_form(self):
        """gates=x,y comma form parses to a two-element list."""
        text = "<!-- @gate id=g2 gates=downstream-a,downstream-b -->\n"
        gates, _ = KB_GRAPH._parse_inline_gates(text)
        assert sorted(gates[0]["gates"]) == ["downstream-a", "downstream-b"]

    def test_criteria_captured(self):
        """- [ ] lines immediately after the marker are captured as criteria."""
        text = (
            "<!-- @gate id=g3 status=open -->\n"
            "- [ ] First criterion\n"
            "- [x] Already done\n"
            "- [ ] Third criterion\n"
            "\n"
            "Some prose after blank line (not a criterion).\n"
        )
        gates, _ = KB_GRAPH._parse_inline_gates(text)
        assert len(gates) == 1
        assert gates[0]["criteria"] == [
            "First criterion",
            "Already done",
            "Third criterion",
        ]

    def test_criteria_stop_at_blank_line(self):
        """Criteria collection stops at the first blank line."""
        text = (
            "<!-- @gate id=g4 -->\n"
            "- [ ] Criterion one\n"
            "\n"
            "- [ ] Should NOT be captured (after blank)\n"
        )
        gates, _ = KB_GRAPH._parse_inline_gates(text)
        assert gates[0]["criteria"] == ["Criterion one"]

    def test_criteria_stop_at_heading(self):
        """Criteria collection stops at a Markdown heading line."""
        text = (
            "<!-- @gate id=g5 -->\n"
            "- [ ] Criterion one\n"
            "## Next Section\n"
            "- [ ] Not a criterion\n"
        )
        gates, _ = KB_GRAPH._parse_inline_gates(text)
        assert gates[0]["criteria"] == ["Criterion one"]

    def test_no_false_positive_on_bare_gate_prose(self):
        """Bare 'gate' prose (no sigil) must produce zero detected gates."""
        text = (
            "This note discusses the concept of a gate review.\n"
            "The gate criteria are important for the project.\n"
            "Meeting the gate threshold is required before launch.\n"
        )
        gates, skipped = KB_GRAPH._parse_inline_gates(text)
        assert gates == [], f"bare 'gate' prose must not trigger detection; got {gates}"
        assert skipped == 0

    def test_malformed_marker_missing_id_skipped(self):
        """A marker without an 'id' attr is skipped and counted in skipped."""
        text = "<!-- @gate status=open blocking=true gates=x -->\n"
        gates, skipped = KB_GRAPH._parse_inline_gates(text)
        assert gates == [], "missing-id marker must produce no gate"
        assert skipped == 1, f"expected skipped=1, got {skipped}"

    def test_malformed_marker_no_crash(self, tmp_path):
        """build_graph_facts must not crash when a marker is malformed."""
        note = _make_note_with_text(
            "proj/bad-gate.md", "bad gate host",
            "project",
            "---\ntype: project\ntitle: \"bad gate host\"\n---\n"
            "<!-- @gate status=open -->\n",  # no id
        )
        notes = [note]
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["gate_skipped"] == 1
        assert stats["gate_count"] == 0

    def test_fenced_code_block_gate_not_detected(self):
        """A gate marker inside a fenced code block must produce ZERO detected gates.

        This is the documentation-example case: real declarations are unfenced
        HTML comments; fenced examples are inert documentation, not declarations.
        """
        text = (
            "# Proposal\n"
            "Below is an example of a gate marker:\n"
            "\n"
            "```markdown\n"
            "<!-- @gate id=example-gate status=open blocking=true gates=some-target -->\n"
            "```\n"
            "\n"
            "And a tilde-fenced variant:\n"
            "\n"
            "~~~\n"
            "<!-- @gate id=tilde-gate status=open -->\n"
            "~~~\n"
            "\n"
            "That's how you write one.\n"
        )
        gates, skipped = KB_GRAPH._parse_inline_gates(text)
        assert gates == [], (
            f"gate markers inside fenced code blocks must not be detected; got {gates}"
        )
        assert skipped == 0

    def test_gate_after_fence_detected(self):
        """A gate marker AFTER a fenced block (fence closed) IS detected."""
        text = (
            "```\n"
            "some code here\n"
            "```\n"
            "<!-- @gate id=real-gate status=open -->\n"
        )
        gates, skipped = KB_GRAPH._parse_inline_gates(text)
        assert len(gates) == 1, f"gate after closed fence must be detected; got {gates}"
        assert gates[0]["id"] == "real-gate"


class TestInlineGateGraphIntegration:
    """Inline gate markers must produce gate nodes and correct graph edges."""

    def _build_notes_with_gate(self) -> list[dict]:
        """
        note-alpha: upstream prereq note.
        note-beta:  downstream note that the gate blocks.
        Gate g-test: requires=[note-alpha], gates=[note-beta]

        Expected edges:
          note-alpha -> g-test  "requires"   (alpha is prereq of gate)
          g-test     -> note-beta  "gates"   (gate blocks beta)
        """
        return [
            _make_note_with_text(
                "nodes/note-alpha.md", "note-alpha", "project",
                "---\ntype: project\ntitle: \"note-alpha\"\n---\n"
                "<!-- @gate id=g-test status=open blocking=true "
                "gates=note-beta requires=[note-alpha] -->\n"
                "- [ ] Alpha must be complete\n",
            ),
            _make_note("nodes/note-beta.md", "note-beta"),
        ]

    def _run(self, tmp_path):
        notes = self._build_notes_with_gate()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats

    def test_gate_node_in_graph_facts(self, tmp_path):
        """The gate-id must appear as a slug in graph_facts."""
        conn, stats = self._run(tmp_path)
        row = conn.execute(
            "SELECT slug FROM graph_facts WHERE slug = 'g-test'"
        ).fetchone()
        assert row is not None, "gate-id 'g-test' must appear in graph_facts"

    def test_gate_count_in_stats(self, tmp_path):
        """stats['gate_count'] must be 1 for a single valid inline gate."""
        _, stats = self._run(tmp_path)
        assert stats["gate_count"] == 1, f"expected gate_count=1, got {stats}"

    def test_gates_edge_emitted(self, tmp_path):
        """Edge gate -> note-beta type 'gates' must be in the edges table."""
        conn, _ = self._run(tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='g-test' AND type='gates'"
        ).fetchone()
        assert row is not None, "expected gate->note-beta 'gates' edge"
        assert row[1] == "nodes/note-beta.md", (
            f"expected dst=nodes/note-beta.md, got {row[1]}"
        )

    def test_requires_edge_emitted(self, tmp_path):
        """Edge note-alpha -> gate type 'requires' must be in the edges table."""
        conn, _ = self._run(tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE dst='g-test' AND type='requires'"
        ).fetchone()
        assert row is not None, "expected note-alpha->g-test 'requires' edge"
        assert row[0] == "nodes/note-alpha.md", (
            f"expected src=nodes/note-alpha.md, got {row[0]}"
        )

    def test_gate_node_in_graph_json(self, tmp_path):
        """Gate node appears in graph.json with type='gate'."""
        conn, _ = self._run(tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        gate_nodes = [n for n in data["nodes"] if n["id"] == "g-test"]
        assert len(gate_nodes) == 1, "gate node must appear exactly once in graph.json"
        g = gate_nodes[0]
        assert g["type"] == "gate"
        assert g["status"] == "open"
        assert g["blocking"] is True
        assert g["criteria_count"] == 1  # "- [ ] Alpha must be complete"

    def test_gate_node_has_required_base_fields(self, tmp_path):
        """Gate node in graph.json has all fields required by existing tests."""
        conn, _ = self._run(tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        gate_nodes = [n for n in data["nodes"] if n["id"] == "g-test"]
        assert gate_nodes, "gate node missing from graph.json"
        required = {"id", "label", "type", "rag", "group", "topo_rank", "cycle_id", "goal", "next", "blocker"}
        missing = required - set(gate_nodes[0].keys())
        assert not missing, f"gate node missing base fields: {missing}"

    def test_dangling_gate_target_counted_not_crashed(self, tmp_path):
        """A gate referencing a slug that doesn't exist: gate node kept, edge dropped, dangling counted."""
        note = _make_note_with_text(
            "proj/host.md", "host", "project",
            "---\ntype: project\ntitle: \"host\"\n---\n"
            "<!-- @gate id=g-dangle gates=nonexistent-slug -->\n",
        )
        notes = [note]
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        # Gate node must exist in graph_facts.
        row = conn.execute(
            "SELECT slug FROM graph_facts WHERE slug='g-dangle'"
        ).fetchone()
        assert row is not None, "dangling-target gate node must still be in graph_facts"
        # The dangling target edge must NOT appear in the edges table.
        edge_rows = conn.execute(
            "SELECT * FROM edges WHERE src='g-dangle'"
        ).fetchall()
        assert edge_rows == [], f"dangling gate edge must not be inserted: {edge_rows}"
        # Dangling count must be >= 1.
        assert stats["dangling_dropped"] >= 1


class TestGateArtifactNote:
    """type:gate frontmatter notes treated as gate artifact nodes."""

    def _build_artifact_notes(self) -> list[dict]:
        """
        gate-note: type:gate, gates: [note-downstream], requires: [note-upstream]
        note-upstream, note-downstream: plain project notes.
        """
        gate_text = (
            "---\n"
            "type: gate\n"
            "title: \"Phase Gate\"\n"
            "gate-id: phase-gate-1\n"
            "status: open\n"
            "blocking: true\n"
            "gates: [\"[[note-downstream]]\"]\n"
            "requires: [\"[[note-upstream]]\"]\n"
            "---\n"
            "# Phase Gate\n"
        )
        return [
            {"path": "gates/phase-gate.md", "fm": {
                "type": "gate", "title": "Phase Gate",
                "gate-id": "phase-gate-1", "status": "open", "blocking": True,
            }, "text": gate_text},
            _make_note("nodes/note-upstream.md", "note-upstream"),
            _make_note("nodes/note-downstream.md", "note-downstream"),
        ]

    def _run(self, tmp_path):
        notes = self._build_artifact_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        return conn, stats

    def test_artifact_gate_node_in_graph_facts(self, tmp_path):
        """Artifact gate must appear as a path-keyed node in graph_facts."""
        conn, _ = self._run(tmp_path)
        row = conn.execute(
            "SELECT slug FROM graph_facts WHERE slug='gates/phase-gate.md'"
        ).fetchone()
        assert row is not None, "artifact gate node must be in graph_facts by path"

    def test_artifact_gates_edge(self, tmp_path):
        """gates: field on an artifact gate produces gate->downstream 'gates' edge."""
        conn, _ = self._run(tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='gates/phase-gate.md' AND type='gates'"
        ).fetchone()
        assert row is not None, "artifact gate must emit a 'gates' edge"
        assert row[1] == "nodes/note-downstream.md"

    def test_artifact_requires_edge(self, tmp_path):
        """requires: field on an artifact gate produces upstream->gate 'requires' edge."""
        conn, _ = self._run(tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE dst='gates/phase-gate.md' AND type='requires'"
        ).fetchone()
        assert row is not None, "artifact gate must have a 'requires' inbound edge"
        assert row[0] == "nodes/note-upstream.md"

    def test_artifact_gate_json_node_type(self, tmp_path):
        """Artifact gate node in graph.json has type='gate'."""
        conn, _ = self._run(tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        gn = next((n for n in data["nodes"] if n["id"] == "gates/phase-gate.md"), None)
        assert gn is not None, "artifact gate must appear in graph.json"
        assert gn["type"] == "gate"

    def test_artifact_gate_criteria_from_raw_text(self, tmp_path):
        """criteria_count for an artifact gate is read from fm_text (not fm dict).

        kb-index's parse_frontmatter skips list fields, so the fm dict won't have
        criteria populated.  Verifies that criteria_count > 0 when the raw text
        has a `criteria:` block list — even if fm dict doesn't contain the key.
        """
        gate_text = (
            "---\n"
            "type: gate\n"
            "title: \"Rich Gate\"\n"
            "gate-id: rich-gate\n"
            "status: open\n"
            "blocking: true\n"
            "gates: [\"[[note-downstream]]\"]\n"
            "requires: [\"[[note-upstream]]\"]\n"
            "criteria:\n"
            "  - First acceptance criterion\n"
            "  - Second acceptance criterion\n"
            "---\n"
            "# Rich Gate\n"
        )
        # Build fm dict WITHOUT criteria (simulating kb-index parse_frontmatter
        # which skips list fields).
        notes = [
            {"path": "gates/rich-gate.md", "fm": {
                "type": "gate", "title": "Rich Gate",
                "gate-id": "rich-gate", "status": "open", "blocking": True,
                # No 'criteria' key — simulating the real kb-index output.
            }, "text": gate_text},
            _make_note("nodes/note-upstream.md", "note-upstream"),
            _make_note("nodes/note-downstream.md", "note-downstream"),
        ]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        gn = next((n for n in data["nodes"] if n["id"] == "gates/rich-gate.md"), None)
        assert gn is not None, "artifact gate must appear in graph.json"
        assert gn["criteria_count"] == 2, (
            f"criteria_count must be parsed from raw fm_text, not fm dict; got {gn['criteria_count']}"
        )
        assert gn["blocking"] is True, f"blocking must be parsed from fm_text; got {gn['blocking']}"


class TestGateInCycle:
    """A gate participating in a cycle must be detected by Tarjan SCC."""

    def _build_cycle_with_gate(self) -> list[dict]:
        """
        note-x requires g-cycle-gate (via inline marker on note-x).
        g-cycle-gate gates note-x  → creates a 2-node cycle.

        Inline marker on note-x:
          <!-- @gate id=g-cycle-gate gates=note-x requires=[note-x] -->

        Edge directions:
          note-x -> g-cycle-gate  "requires"  (g-cycle-gate is prereq of note-x)
          g-cycle-gate -> note-x  "gates"     (gate blocks note-x)
        Together this forms the cycle:  note-x <-> g-cycle-gate.
        """
        return [
            _make_note_with_text(
                "cycle/note-x.md", "note-x", "project",
                "---\ntype: project\ntitle: \"note-x\"\n---\n"
                "<!-- @gate id=g-cycle-gate gates=note-x requires=[note-x] -->\n",
            ),
        ]

    def test_gate_cycle_detected(self, tmp_path):
        """A gate forming a 2-node cycle with a note must be flagged by Tarjan."""
        notes = self._build_cycle_with_gate()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["cycle_count"] >= 1, (
            f"expected at least 1 cycle (gate+note cycle); got {stats['cycle_count']}"
        )
        # Both nodes must share a cycle_id.
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts "
            "WHERE slug IN ('cycle/note-x.md', 'g-cycle-gate')"
        ).fetchall()
        assert len(rows) == 2, f"both cycle members must be in graph_facts; got {rows}"
        cids = {r[1] for r in rows}
        assert None not in cids, f"cycle members must have non-NULL cycle_id; got {rows}"
        assert len(cids) == 1, f"both members must share one cycle_id; got {cids}"


class TestGateZeroImpactOnExistingNotes:
    """With no @gate markers in the vault, behaviour is identical to pre-gate code."""

    def test_existing_fixture_unaffected(self, tmp_path):
        """The original 6-note fixture must produce the same stats when no gates exist."""
        notes = _build_fixture_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        # Gate stats added cleanly.
        assert stats["gate_count"] == 0
        assert stats["gate_skipped"] == 0
        # Core stats unaffected.
        assert stats["cycle_count"] == 1
        assert stats["node_count"] == len(notes)


# ---------------------------------------------------------------------------
# NEW: Sidecar edge integration (project-edges.yaml → graph edges)
# ---------------------------------------------------------------------------

class TestSidecarEdges:
    """project-edges.yaml sidecar must produce first-class requires edges in graph.

    Topology:
        projY  →  projX  "requires"   (sidecar: projX: requires: [projY])

    Both projects are path-keyed nodes; no gate involved.
    Sidecar entry uses the vault-relative path as key (canonical node-id).
    """

    def _build_notes(self) -> list[dict]:
        return [
            _make_note("02-projects/1.0-dev/projX.md", "projX"),
            _make_note("02-projects/1.0-dev/projY.md", "projY"),
        ]

    def _write_sidecar(self, tmp_path: Path, content: str) -> None:
        sidecar_dir = tmp_path / "00-meta"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / "project-edges.yaml").write_text(
            content, encoding="utf-8"
        )

    def test_sidecar_requires_edge_in_edges_table(self, tmp_path):
        """Sidecar `projX: requires: [projY]` must produce edge projY→projX type 'requires'."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        # Sidecar key is the vault-relative path (canonical node-id).
        self._write_sidecar(
            tmp_path,
            "02-projects/1.0-dev/projX.md:\n"
            '  requires: ["02-projects/1.0-dev/projY.md"]\n',
        )

        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='02-projects/1.0-dev/projY.md' "
            "AND dst='02-projects/1.0-dev/projX.md' "
            "AND type='requires'"
        ).fetchone()
        assert row is not None, (
            "sidecar requires edge projY→projX must appear in the edges table"
        )

    def test_sidecar_edge_by_title(self, tmp_path):
        """Sidecar key resolved by title (res_map contains title→path)."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        # Use title 'projX' as key, and 'projY' as requires target.
        self._write_sidecar(
            tmp_path,
            "projX:\n"
            '  requires: ["projY"]\n',
        )

        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='02-projects/1.0-dev/projY.md' "
            "AND dst='02-projects/1.0-dev/projX.md' "
            "AND type='requires'"
        ).fetchone()
        assert row is not None, (
            "sidecar key resolved by title must produce the correct requires edge"
        )

    def test_sidecar_goal_sets_node_goal(self, tmp_path):
        """Sidecar `goal: true` must set goal=True on the node in graph.json."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        self._write_sidecar(
            tmp_path,
            "02-projects/1.0-dev/projX.md:\n"
            "  requires: []\n"
            "  goal: true\n",
        )

        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)

        projx_node = next(
            (n for n in data["nodes"] if n["id"] == "02-projects/1.0-dev/projX.md"),
            None,
        )
        assert projx_node is not None
        assert projx_node["goal"] is True, (
            "sidecar goal:true must set goal=True on the node in graph.json"
        )

    def test_sidecar_absent_no_effect(self, tmp_path):
        """Without a sidecar file, build_graph_facts behaves identically to before."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        # No sidecar file created.
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        # No requires edges (notes have no requires in their frontmatter).
        req_edges = conn.execute(
            "SELECT * FROM edges WHERE type='requires'"
        ).fetchall()
        assert req_edges == [], f"no sidecar → no requires edges; got {req_edges}"

    def test_sidecar_dangling_target_counted_not_crashed(self, tmp_path):
        """A sidecar requires target that can't be resolved is counted as dangling."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        self._write_sidecar(
            tmp_path,
            "02-projects/1.0-dev/projX.md:\n"
            '  requires: ["nonexistent-slug"]\n',
        )

        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["dangling_dropped"] >= 1, (
            "unresolvable sidecar requires target must be counted as dangling"
        )

    def test_sidecar_edge_in_graph_json(self, tmp_path):
        """Sidecar-derived edge must appear in graph.json edges array."""
        notes = self._build_notes()
        conn = _setup_db(notes)

        self._write_sidecar(
            tmp_path,
            "02-projects/1.0-dev/projX.md:\n"
            '  requires: ["02-projects/1.0-dev/projY.md"]\n',
        )

        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)

        matching = [
            e for e in data["edges"]
            if e["s"] == "02-projects/1.0-dev/projY.md"
            and e["d"] == "02-projects/1.0-dev/projX.md"
            and e["t"] == "requires"
        ]
        assert len(matching) == 1, (
            f"sidecar requires edge must appear in graph.json; found {matching}"
        )

    def test_sidecar_idempotent_double_run(self, tmp_path):
        """Running build_graph_facts twice with the same sidecar is idempotent."""
        notes = self._build_notes()

        self._write_sidecar(
            tmp_path,
            "02-projects/1.0-dev/projX.md:\n"
            '  requires: ["02-projects/1.0-dev/projY.md"]\n'
            "  goal: true\n",
        )

        conn1 = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn1, notes, tmp_path)
        edges1 = sorted(conn1.execute("SELECT src, dst, type FROM edges").fetchall())
        conn1.close()

        conn2 = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn2, notes, tmp_path)
        edges2 = sorted(conn2.execute("SELECT src, dst, type FROM edges").fetchall())
        conn2.close()

        assert edges1 == edges2, "sidecar edge injection must be idempotent"


# ---------------------------------------------------------------------------
# NEW: Task 10 — supersedes/partof in the sidecar (Part A)
# ---------------------------------------------------------------------------

class TestSidecarSupersedes:
    """Sidecar `supersedes: [T]` under project X must produce edge T→X "supersedes".

    Direction matches _extract_edges: `supersedes: M` on note N → _add(M, N, "supersedes")
    → edge src=M, dst=N.  So sidecar supersedes: [OldProj] under NewProj → (OldProj→NewProj).
    """

    def _build_notes(self) -> list[dict]:
        return [
            _make_note("02-projects/1.0-dev/newproj.md", "newproj"),
            _make_note("02-projects/1.0-dev/oldproj.md", "oldproj"),
        ]

    def _write_sidecar(self, tmp_path: Path, content: str) -> None:
        sidecar_dir = tmp_path / "00-meta"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / "project-edges.yaml").write_text(content, encoding="utf-8")

    def test_sidecar_supersedes_edge_in_edges_table(self, tmp_path):
        """Sidecar `newproj: supersedes: [oldproj]` → edge oldproj→newproj "supersedes"."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "newproj:\n"
            '  supersedes: ["oldproj"]\n',
        )
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='02-projects/1.0-dev/oldproj.md' "
            "AND dst='02-projects/1.0-dev/newproj.md' "
            "AND type='supersedes'"
        ).fetchone()
        assert row is not None, (
            "sidecar supersedes edge oldproj→newproj must appear in the edges table"
        )

    def test_sidecar_supersedes_edge_in_graph_json(self, tmp_path):
        """Sidecar supersedes edge must appear in graph.json."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "newproj:\n"
            '  supersedes: ["oldproj"]\n',
        )
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        import json as _json
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = _json.load(f)
        matching = [
            e for e in data["edges"]
            if e["s"] == "02-projects/1.0-dev/oldproj.md"
            and e["d"] == "02-projects/1.0-dev/newproj.md"
            and e["t"] == "supersedes"
        ]
        assert len(matching) == 1, (
            f"sidecar supersedes edge must appear in graph.json; found {matching}"
        )

    def test_sidecar_supersedes_direction_matches_extract_edges(self, tmp_path):
        """Direction: sidecar supersedes[T] → (T→proj) same as frontmatter supersedes:[[T]]."""
        # Also verify via frontmatter: a note with supersedes: "[[oldproj]]" produces
        # the same (oldproj→newproj) direction as the sidecar path.
        notes = [
            _make_note(
                "02-projects/1.0-dev/newproj.md", "newproj",
                extra_fm='supersedes: "[[oldproj]]"\n',
            ),
            _make_note("02-projects/1.0-dev/oldproj.md", "oldproj"),
        ]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        row = conn.execute(
            "SELECT src, dst FROM edges WHERE type='supersedes'"
        ).fetchone()
        assert row == ("02-projects/1.0-dev/oldproj.md", "02-projects/1.0-dev/newproj.md"), (
            f"frontmatter supersedes direction must be oldproj→newproj; got {row}"
        )

    def test_sidecar_supersedes_dangling_counted_not_crashed(self, tmp_path):
        """Unresolvable supersedes target → dangling count incremented, no crash."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "newproj:\n"
            '  supersedes: ["ghost-proj"]\n',
        )
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["dangling_dropped"] >= 1, (
            "unresolvable sidecar supersedes target must be counted as dangling"
        )


class TestSidecarPartof:
    """Sidecar `partof: [T]` under project X must produce edge T→X "partof".

    Direction matches _extract_edges: `up: P` on note N → _add(P, N, "partof")
    → edge src=P, dst=N.  So sidecar partof: [ParentProj] under ChildProj → (ParentProj→ChildProj).
    """

    def _build_notes(self) -> list[dict]:
        return [
            _make_note("02-projects/1.0-dev/childproj.md", "childproj"),
            _make_note("02-projects/1.0-dev/parentproj.md", "parentproj"),
        ]

    def _write_sidecar(self, tmp_path: Path, content: str) -> None:
        sidecar_dir = tmp_path / "00-meta"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / "project-edges.yaml").write_text(content, encoding="utf-8")

    def test_sidecar_partof_edge_in_edges_table(self, tmp_path):
        """Sidecar `childproj: partof: [parentproj]` → edge parentproj→childproj "partof"."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "childproj:\n"
            '  partof: ["parentproj"]\n',
        )
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        row = conn.execute(
            "SELECT src, dst, type FROM edges "
            "WHERE src='02-projects/1.0-dev/parentproj.md' "
            "AND dst='02-projects/1.0-dev/childproj.md' "
            "AND type='partof'"
        ).fetchone()
        assert row is not None, (
            "sidecar partof edge parentproj→childproj must appear in the edges table"
        )

    def test_sidecar_partof_dangling_counted_not_crashed(self, tmp_path):
        """Unresolvable partof target → dangling count incremented, no crash."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "childproj:\n"
            '  partof: ["ghost-parent"]\n',
        )
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["dangling_dropped"] >= 1, (
            "unresolvable sidecar partof target must be counted as dangling"
        )

    def test_sidecar_partof_edge_in_graph_json(self, tmp_path):
        """Sidecar partof edge must appear in graph.json."""
        notes = self._build_notes()
        conn = _setup_db(notes)
        self._write_sidecar(
            tmp_path,
            "childproj:\n"
            '  partof: ["parentproj"]\n',
        )
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        matching = [
            e for e in data["edges"]
            if e["s"] == "02-projects/1.0-dev/parentproj.md"
            and e["d"] == "02-projects/1.0-dev/childproj.md"
            and e["t"] == "partof"
        ]
        assert len(matching) == 1, (
            f"sidecar partof edge must appear in graph.json; found {matching}"
        )

    def test_sidecar_partof_direction_matches_extract_edges(self, tmp_path):
        """Direction: sidecar partof[P] → (P→child) same as frontmatter up:[[P]]."""
        # Verify via frontmatter: a note with up: "[[parentproj]]" produces
        # the same (parentproj→childproj) direction as the sidecar path.
        notes = [
            _make_note(
                "02-projects/1.0-dev/childproj.md", "childproj",
                extra_fm='up: "[[parentproj]]"\n',
            ),
            _make_note("02-projects/1.0-dev/parentproj.md", "parentproj"),
        ]
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        row = conn.execute(
            "SELECT src, dst FROM edges WHERE type='partof'"
        ).fetchone()
        assert row == ("02-projects/1.0-dev/parentproj.md", "02-projects/1.0-dev/childproj.md"), (
            f"frontmatter up: direction must be parentproj→childproj; got {row}"
        )


# ---------------------------------------------------------------------------
# NEW: Objectives sidecar (00-meta/objectives.yaml)
# ---------------------------------------------------------------------------

def test_read_objectives_parses_nodes_and_advances(tmp_path):
    (tmp_path / "00-meta").mkdir(parents=True)
    (tmp_path / "00-meta" / "objectives.yaml").write_text(
        'objectives:\n'
        '  objective-a:\n    label: "Own the market"\n    kind: ultimate\n'
        '  objective-b:\n    label: "Detection GA"\n    kind: milestone\n    advances: ["objective-a"]\n'
        '  no-kind-obj:\n    label: "Defaults milestone"\n'
        'project_advances:\n  projbeta: ["objective-b"]\n',
        encoding="utf-8")
    obj = KB_GRAPH._read_objectives(tmp_path)
    assert obj["objectives"]["objective-a"]["kind"] == "ultimate"
    assert obj["objectives"]["objective-b"]["advances"] == ["objective-a"]
    assert obj["objectives"]["no-kind-obj"]["kind"] == "milestone"  # default
    assert obj["project_advances"]["projbeta"] == ["objective-b"]


def test_read_objectives_absent_file(tmp_path):
    assert KB_GRAPH._read_objectives(tmp_path) == {"objectives": {}, "project_advances": {}}


# ---------------------------------------------------------------------------
# FIX tests: trailing inline comments + annotation + docstring + edge cases
# ---------------------------------------------------------------------------

class TestReadObjectivesTrailingComments:
    """FIX 1 — trailing inline comments must not corrupt data or drop sections."""

    def _write(self, tmp_path: "Path", content: str) -> None:
        (tmp_path / "00-meta").mkdir(parents=True, exist_ok=True)
        (tmp_path / "00-meta" / "objectives.yaml").write_text(content, encoding="utf-8")

    def test_section_header_with_trailing_comment_not_dropped(self, tmp_path):
        """objectives:  # main section — section must still be recognized and parsed."""
        self._write(tmp_path, (
            "objectives:  # hierarchy\n"
            "  keep-me:\n"
            '    label: "Keep me"\n'
            "    kind: ultimate\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert "keep-me" in obj["objectives"], (
            "section header with trailing comment must not drop the whole section"
        )
        assert obj["objectives"]["keep-me"]["kind"] == "ultimate"

    def test_kind_trailing_comment_stripped(self, tmp_path):
        """kind: ultimate   # apex note — stored kind must be 'ultimate' not 'ultimate   # apex note'."""
        self._write(tmp_path, (
            "objectives:\n"
            "  obj-a:\n"
            '    label: "Obj A"\n'
            "    kind: ultimate   # apex\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["obj-a"]["kind"] == "ultimate", (
            f"trailing comment on kind must be stripped; got {obj['objectives']['obj-a']['kind']!r}"
        )

    def test_label_with_hash_inside_quotes_preserved(self, tmp_path):
        """label: "C# guide" — the hash is INSIDE the quotes and must not be stripped."""
        self._write(tmp_path, (
            "objectives:\n"
            "  csharp-guide:\n"
            '    label: "C# guide"\n'
            "    kind: milestone\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["csharp-guide"]["label"] == "C# guide", (
            f"hash inside quoted label must not be stripped; "
            f"got {obj['objectives']['csharp-guide']['label']!r}"
        )

    def test_label_with_space_hash_inside_quotes_preserved(self, tmp_path):
        """label: "C # guide" — space-hash inside the quotes must not be stripped.

        This is the discriminating case: a naive ' #'-split would truncate to 'C'.
        """
        self._write(tmp_path, (
            "objectives:\n"
            "  spaced-hash:\n"
            '    label: "C # guide"\n'
            "    kind: milestone\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["spaced-hash"]["label"] == "C # guide", (
            f"space-hash inside quoted label must not be stripped; "
            f"got {obj['objectives']['spaced-hash']['label']!r}"
        )

    def test_advances_trailing_comment_stripped(self, tmp_path):
        """advances: ["slug-a"]  # inline list — comment after ] must not corrupt the list."""
        self._write(tmp_path, (
            "objectives:\n"
            "  obj-b:\n"
            '    label: "Obj B"\n'
            '    advances: ["slug-a"]  # inline list\n'
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["obj-b"]["advances"] == ["slug-a"], (
            f"trailing comment after advances list must be stripped; "
            f"got {obj['objectives']['obj-b']['advances']!r}"
        )

    def test_project_advances_trailing_comment_stripped(self, tmp_path):
        """project_advances value line with trailing comment after ] must parse correctly."""
        self._write(tmp_path, (
            "project_advances:\n"
            '  my-project: ["obj-a"]  # see objectives\n'
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["project_advances"]["my-project"] == ["obj-a"], (
            f"trailing comment after project_advances list must be stripped; "
            f"got {obj['project_advances']['my-project']!r}"
        )

    def test_interleaved_blank_lines_tolerated(self, tmp_path):
        """Blank lines between objective entries must be tolerated."""
        self._write(tmp_path, (
            "objectives:\n"
            "\n"
            "  obj-one:\n"
            '    label: "One"\n'
            "    kind: milestone\n"
            "\n"
            "  obj-two:\n"
            '    label: "Two"\n'
            "    kind: ultimate\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert "obj-one" in obj["objectives"]
        assert "obj-two" in obj["objectives"]
        assert obj["objectives"]["obj-two"]["kind"] == "ultimate"

    def test_explicit_empty_advances_yields_empty_list(self, tmp_path):
        """advances: [] must yield an empty list, not a parse error."""
        self._write(tmp_path, (
            "objectives:\n"
            "  obj-empty:\n"
            '    label: "Empty advances"\n'
            "    advances: []\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["obj-empty"]["advances"] == [], (
            f"explicit empty advances must yield []; got {obj['objectives']['obj-empty']['advances']!r}"
        )

    def test_no_advances_key_yields_empty_list_default(self, tmp_path):
        """A slug with no 'advances:' key must default to advances=[]."""
        self._write(tmp_path, (
            "objectives:\n"
            "  obj-no-adv:\n"
            '    label: "No advances key"\n'
            "    kind: milestone\n"
        ))
        obj = KB_GRAPH._read_objectives(tmp_path)
        assert obj["objectives"]["obj-no-adv"]["advances"] == [], (
            f"missing advances key must default to []; "
            f"got {obj['objectives']['obj-no-adv']['advances']!r}"
        )


# ---------------------------------------------------------------------------
# NEW: Task 2 — Objective nodes + `advances` edges
# ---------------------------------------------------------------------------

class TestObjectiveNodesAndAdvancesEdges:
    """build_graph_facts must emit objective nodes and 'advances' edges.

    Vault topology:
        objectives.yaml:
            objective-a  (kind=ultimate)
            objective-b   (kind=milestone, advances=[objective-a])
        project_advances:
            projbeta: [objective-b]

        notes: one project card titled 'projbeta'.

    Expected objective nodes in graph.json:
        id="obj:objective-a", type="objective", kind="ultimate", goal=True
        id="obj:objective-b",  type="objective", kind="milestone", goal=False

    Expected advances edges in graph.json / edges table:
        (src=obj:objective-b, dst=obj:objective-a, t=advances)
        (src=<projbeta node id>,     dst=obj:objective-b,  t=advances)

    Topo chain (advances is prereq-type):
        projbeta (rank 0) -> objective-b (rank 1) -> objective-a (rank 2)
        rank(projbeta) < rank(objective-b) < rank(objective-a)
    """

    def _write_objectives(self, tmp_path: Path) -> None:
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  objective-a:\n"
            '    label: "Own the app-stack market"\n'
            "    kind: ultimate\n"
            "  objective-b:\n"
            '    label: "Detection Engine GA"\n'
            "    kind: milestone\n"
            '    advances: ["objective-a"]\n'
            "project_advances:\n"
            '  projbeta: ["objective-b"]\n',
            encoding="utf-8",
        )

    def _build_notes(self) -> list[dict]:
        return [
            _make_note(
                "02-projects/1.0-dev/projbeta.md", "projbeta"
            ),
        ]

    def _run(self, tmp_path: Path):
        self._write_objectives(tmp_path)
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        return conn, data

    # --- AC 1: objective nodes ---

    def test_ultimate_objective_node_exists(self, tmp_path):
        """obj:objective-a node exists with type=objective, kind=ultimate, goal=True."""
        _, data = self._run(tmp_path)
        nodes_by_id = {n["id"]: n for n in data["nodes"]}
        assert "obj:objective-a" in nodes_by_id, (
            "ultimate objective node must be in graph.json"
        )
        n = nodes_by_id["obj:objective-a"]
        assert n["type"] == "objective", f"expected type=objective; got {n['type']!r}"
        assert n["kind"] == "ultimate", f"expected kind=ultimate; got {n['kind']!r}"
        assert n["goal"] is True, f"ultimate objective must have goal=True; got {n['goal']!r}"

    def test_milestone_objective_node_exists(self, tmp_path):
        """obj:objective-b node exists with type=objective, kind=milestone, goal=False."""
        _, data = self._run(tmp_path)
        nodes_by_id = {n["id"]: n for n in data["nodes"]}
        assert "obj:objective-b" in nodes_by_id, (
            "milestone objective node must be in graph.json"
        )
        n = nodes_by_id["obj:objective-b"]
        assert n["type"] == "objective"
        assert n["kind"] == "milestone"
        assert n["goal"] is False, f"milestone objective must have goal=False; got {n['goal']!r}"

    def test_objective_nodes_have_all_base_fields(self, tmp_path):
        """Objective nodes must carry all 10 base fields (same set as project/gate nodes)."""
        _, data = self._run(tmp_path)
        required = {"id", "label", "type", "rag", "group", "topo_rank", "cycle_id", "goal", "next", "blocker"}
        for n in data["nodes"]:
            if n["type"] == "objective":
                missing = required - set(n.keys())
                assert not missing, f"objective node {n['id']!r} missing fields: {missing}"

    # --- AC 2: advances edges ---

    def test_milestone_to_ultimate_advances_edge(self, tmp_path):
        """Edge src=obj:objective-b -> dst=obj:objective-a, t=advances."""
        _, data = self._run(tmp_path)
        edge = next(
            (e for e in data["edges"]
             if e["s"] == "obj:objective-b"
             and e["d"] == "obj:objective-a"
             and e["t"] == "advances"),
            None,
        )
        assert edge is not None, (
            "milestone→ultimate advances edge must appear in graph.json"
        )

    def test_project_to_objective_advances_edge(self, tmp_path):
        """Edge src=<projbeta node id> -> dst=obj:objective-b, t=advances."""
        _, data = self._run(tmp_path)
        # projbeta node id is the vault-relative path
        avv_id = "02-projects/1.0-dev/projbeta.md"
        edge = next(
            (e for e in data["edges"]
             if e["s"] == avv_id
             and e["d"] == "obj:objective-b"
             and e["t"] == "advances"),
            None,
        )
        assert edge is not None, (
            f"project→objective advances edge ({avv_id!r}→obj:objective-b) "
            "must appear in graph.json"
        )

    # --- AC 3: advances in _PREREQ_TYPES → drives topo_rank ---

    def test_advances_in_prereq_types(self, tmp_path):
        """'advances' must be in _PREREQ_TYPES."""
        assert "advances" in KB_GRAPH._PREREQ_TYPES, (
            "'advances' must be in _PREREQ_TYPES so it participates in topo ranking"
        )

    def test_topo_rank_chain(self, tmp_path):
        """Topo rank must propagate through advances edges:
        rank(projbeta) < rank(milestone) < rank(ultimate).
        """
        _, data = self._run(tmp_path)
        by_id = {n["id"]: n["topo_rank"] for n in data["nodes"]}
        avv_id = "02-projects/1.0-dev/projbeta.md"
        rank_avv = by_id.get(avv_id)
        rank_ms = by_id.get("obj:objective-b")
        rank_ult = by_id.get("obj:objective-a")
        assert rank_avv is not None and rank_ms is not None and rank_ult is not None, (
            f"all three nodes must have topo_rank; got avv={rank_avv}, ms={rank_ms}, ult={rank_ult}"
        )
        assert rank_avv < rank_ms, (
            f"projbeta (advancer) must rank lower than milestone objective; "
            f"got avv={rank_avv}, ms={rank_ms}"
        )
        assert rank_ms < rank_ult, (
            f"milestone objective (advancer) must rank lower than ultimate objective; "
            f"got ms={rank_ms}, ult={rank_ult}"
        )

    # --- AC 4: unresolvable project title → dangling count, no crash ---

    def test_unresolvable_project_title_increments_dangling(self, tmp_path):
        """An unresolvable project title in project_advances increments dangling, doesn't crash."""
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  some-obj:\n"
            '    label: "Some objective"\n'
            "    kind: ultimate\n"
            "project_advances:\n"
            '  nonexistent-project: ["some-obj"]\n',
            encoding="utf-8",
        )
        notes = self._build_notes()
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        assert stats["dangling_dropped"] >= 1, (
            "unresolvable project title in project_advances must increment dangling_dropped"
        )


# ---------------------------------------------------------------------------
# FIX 1 — objective label fallback: empty label → slug
# ---------------------------------------------------------------------------

class TestObjectiveLabelFallback:
    """An objective with no label: key must emit label == bare slug in graph.json.

    Before FIX 1 the production code does: `"label": obj_fields["label"]`
    which yields "" when the operator omits the label: key.  After the fix it
    does: `obj_fields["label"] or obj_fields["slug"]` — falling back to the
    bare slug.
    """

    def _write_objectives_no_label(self, tmp_path: Path) -> None:
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  no-label-obj:\n"
            "    kind: milestone\n",  # no label: key at all
            encoding="utf-8",
        )

    def test_missing_label_falls_back_to_slug(self, tmp_path):
        """Objective with no label: key must produce label == bare slug, not ''."""
        self._write_objectives_no_label(tmp_path)
        conn = _setup_db([])
        KB_GRAPH.build_graph_facts(conn, [], tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        nodes_by_id = {n["id"]: n for n in data["nodes"]}
        assert "obj:no-label-obj" in nodes_by_id, (
            "objective node obj:no-label-obj must appear in graph.json"
        )
        node = nodes_by_id["obj:no-label-obj"]
        assert node["label"] == "no-label-obj", (
            f"label must fall back to bare slug 'no-label-obj' when label: is omitted; "
            f"got {node['label']!r}"
        )


# ---------------------------------------------------------------------------
# NEW: Task C1 — sidecar lineage fields (advances, phase, milestones)
# ---------------------------------------------------------------------------

def test_read_sidecar_captures_advances_phase(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        "projbeta:\n"
        "  requires: [\"projgamma\"]\n"
        "  advances: projbeta   # lane enum\n"
        "  phase: build\n",
        encoding="utf-8",
    )
    sc = KB_GRAPH._read_sidecar(tmp_path)
    assert sc["projbeta"]["requires"] == ["projgamma"]   # unchanged
    assert sc["projbeta"]["advances"] == "projbeta"
    assert sc["projbeta"]["phase"] == "build"

def test_read_sidecar_parses_milestones_pipe_list(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        "x:\n  milestones: [\"MVP|build|done\", \"Beta|harden|todo\"]\n",
        encoding="utf-8",
    )
    ms = KB_GRAPH._read_sidecar(tmp_path)["x"]["milestones"]
    assert ms == [
        {"title": "MVP", "phase": "build", "status": "done"},
        {"title": "Beta", "phase": "harden", "status": "todo"},
    ]

def test_read_sidecar_defaults_when_absent(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text("y:\n  requires: [\"z\"]\n", encoding="utf-8")
    e = KB_GRAPH._read_sidecar(tmp_path)["y"]
    assert e["advances"] is None and e["phase"] is None and e["milestones"] == []


def test_parse_milestones_single_part(tmp_path):
    """milestones: ["Title"] → single-part item: phase=None, status="todo"."""
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'proj:\n  milestones: ["Title"]\n',
        encoding="utf-8",
    )
    ms = KB_GRAPH._read_sidecar(tmp_path)["proj"]["milestones"]
    assert ms == [{"title": "Title", "phase": None, "status": "todo"}]


def test_parse_milestones_missing_status_defaults_todo(tmp_path):
    """milestones: ["Title|build"] → two-part item: phase="build", status="todo"."""
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'proj:\n  milestones: ["Title|build"]\n',
        encoding="utf-8",
    )
    ms = KB_GRAPH._read_sidecar(tmp_path)["proj"]["milestones"]
    assert ms == [{"title": "Title", "phase": "build", "status": "todo"}]


def test_parse_milestones_blank_title_skipped(tmp_path):
    """milestones: ["|build|done"] → blank title is skipped → []."""
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'proj:\n  milestones: ["|build|done"]\n',
        encoding="utf-8",
    )
    ms = KB_GRAPH._read_sidecar(tmp_path)["proj"]["milestones"]
    assert ms == []


def test_parse_milestones_empty_list(tmp_path):
    """milestones: [] → empty list, no error."""
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        "proj:\n  milestones: []\n",
        encoding="utf-8",
    )
    ms = KB_GRAPH._read_sidecar(tmp_path)["proj"]["milestones"]
    assert ms == []


def test_sidecar_scalar_quoted_value_with_hash_preserved(tmp_path):
    """advances: "C # lane"  # trailing → parsed value is exactly 'C # lane'.

    Regression test for Fix 1: the old _sidecar_scalar calls _strip_trailing_comment
    BEFORE _extract_quoted_label, which truncates "C # lane" to "C (the first ` #`
    inside the quotes is treated as a YAML comment).  The fix routes on quotedness
    first so the hash inside the quotes is preserved and only the trailing comment
    (after the closing quote) is dropped.
    """
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'proj:\n  advances: "C # lane"  # trailing\n',
        encoding="utf-8",
    )
    sc = KB_GRAPH._read_sidecar(tmp_path)
    assert sc["proj"]["advances"] == "C # lane", (
        f"hash inside quotes must be preserved; got {sc['proj']['advances']!r}"
    )

# ---------------------------------------------------------------------------
# FIX 2 — advances cycle detection (objective nodes participating in SCC)
# ---------------------------------------------------------------------------

class TestObjectiveAdvancesCycle:
    """Two milestone objectives each advancing the other → 2-node SCC.

    milestone-a advances milestone-b, milestone-b advances milestone-a →
    edge obj:milestone-a → obj:milestone-b and obj:milestone-b → obj:milestone-a
    (both 'advances', which is in _PREREQ_TYPES) → Tarjan detects a 2-node SCC.

    Mirrors TestGateInCycle harness/style.
    """

    def _write_objectives_cycle(self, tmp_path: Path) -> None:
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  milestone-a:\n"
            '    label: "Milestone A"\n'
            "    kind: milestone\n"
            '    advances: ["milestone-b"]\n'
            "  milestone-b:\n"
            '    label: "Milestone B"\n'
            "    kind: milestone\n"
            '    advances: ["milestone-a"]\n',
            encoding="utf-8",
        )

    def test_cycle_detected(self, tmp_path):
        """Two mutually-advancing milestones must be detected as a cycle."""
        self._write_objectives_cycle(tmp_path)
        conn = _setup_db([])
        stats = KB_GRAPH.build_graph_facts(conn, [], tmp_path)
        assert stats["cycle_count"] >= 1, (
            f"expected at least 1 cycle for mutually-advancing milestones; "
            f"got {stats['cycle_count']}"
        )

    def test_both_nodes_share_cycle_id(self, tmp_path):
        """obj:milestone-a and obj:milestone-b must share a non-null cycle_id."""
        self._write_objectives_cycle(tmp_path)
        conn = _setup_db([])
        KB_GRAPH.build_graph_facts(conn, [], tmp_path)
        rows = conn.execute(
            "SELECT slug, cycle_id FROM graph_facts "
            "WHERE slug IN ('obj:milestone-a', 'obj:milestone-b')"
        ).fetchall()
        assert len(rows) == 2, (
            f"both objective cycle members must be in graph_facts; got {rows}"
        )
        cids = {r[1] for r in rows}
        assert None not in cids, (
            f"cycle members must have non-NULL cycle_id; got {rows}"
        )
        assert len(cids) == 1, (
            f"both members must share one cycle_id; got cids={cids} rows={rows}"
        )


# ---------------------------------------------------------------------------
# FIX 3a — dangling: objective advances → missing target slug
# ---------------------------------------------------------------------------

class TestObjectiveAdvancesDanglingSlug:
    """A milestone's advances list referencing a non-existent slug must increment
    dangling_dropped and not crash; the edge must NOT be inserted.

    Mirrors test_dangling_gate_target_counted_not_crashed style.
    """

    def test_advances_missing_target_counted_not_crashed(self, tmp_path):
        """advances: ["nonexistent-obj"] → dangling increments, no crash, no edge."""
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  solo-milestone:\n"
            '    label: "Solo"\n'
            "    kind: milestone\n"
            '    advances: ["nonexistent-obj"]\n',
            encoding="utf-8",
        )
        conn = _setup_db([])
        stats = KB_GRAPH.build_graph_facts(conn, [], tmp_path)

        # dangling must be counted
        assert stats["dangling_dropped"] >= 1, (
            "advances referencing a missing objective slug must increment dangling_dropped"
        )

        # No edge must be inserted for the missing target
        edge_rows = conn.execute(
            "SELECT * FROM edges WHERE src='obj:solo-milestone'"
        ).fetchall()
        assert edge_rows == [], (
            f"no edge must be inserted when advances target is missing; got {edge_rows}"
        )


# ---------------------------------------------------------------------------
# FIX 3b — dangling: project_advances → missing objective slug (src resolves fine)
# ---------------------------------------------------------------------------

class TestProjectAdvancesDanglingObjSlug:
    """project_advances maps a resolvable project title to a non-existent objective
    slug → dangling_dropped increments, no crash, no edge inserted.

    Key distinction from test_unresolvable_project_title_increments_dangling
    (which tests a missing project title): here the project resolves fine but the
    objective slug is absent from objectives.yaml.

    Mirrors test_dangling_gate_target_counted_not_crashed / test_sidecar_dangling_target_counted_not_crashed.
    """

    def test_project_advances_missing_obj_slug_counted_not_crashed(self, tmp_path):
        """project_advances with a missing obj slug → dangling incremented, no crash."""
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        # objectives.yaml: only "real-obj" defined; project_advances references "ghost-obj"
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  real-obj:\n"
            '    label: "Real"\n'
            "    kind: ultimate\n"
            "project_advances:\n"
            '  projbeta: ["ghost-obj"]\n',
            encoding="utf-8",
        )
        # projbeta note MUST exist so the project title resolves (otherwise we'd
        # hit the already-covered "unresolvable title" branch instead).
        notes = [_make_note("02-projects/1.0-dev/projbeta.md", "projbeta")]
        conn = _setup_db(notes)
        stats = KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        # dangling must be counted (the ghost-obj slug is the missing piece)
        assert stats["dangling_dropped"] >= 1, (
            "project_advances referencing a missing objective slug must increment dangling_dropped"
        )

        # No edge must be inserted for the ghost objective
        avv_path = "02-projects/1.0-dev/projbeta.md"
        edge_rows = conn.execute(
            "SELECT * FROM edges WHERE src=? AND type='advances'",
            (avv_path,),
        ).fetchall()
        assert edge_rows == [], (
            f"no advances edge must be inserted when objective slug is missing; got {edge_rows}"
        )


# ---------------------------------------------------------------------------
# TASK 3 — Transitive blocker → objective rollup (_rollup_blockers_to_objectives)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Unit tests for _rollup_blockers_to_objectives directly (AC4, AC5, and
# isolated AC1/2/3 shapes without full vault overhead).
# ---------------------------------------------------------------------------

def _rollup_node(node_id: str, node_type: str, blocker: "str | None" = None) -> dict:
    """Build a minimal node dict for _rollup_blockers_to_objectives unit tests."""
    return {"id": node_id, "type": node_type, "blocker": blocker}


class TestRollupBlockersDirectUnit:
    """Direct unit tests for _rollup_blockers_to_objectives(nodes, edges).

    Covers cycle-safety, no-blocker baseline, and a diamond-path minimum-distance
    case — all without invoking build_graph_facts.
    """

    def test_rollup_no_blocker_yields_empty(self):
        """AC5: A node with no blocker must have blocks_objectives=[]."""
        nodes = [
            _rollup_node("proj-a.md", "project", blocker=None),
            _rollup_node("obj:goal-x", "objective"),
        ]
        edges = [
            ("proj-a.md", "obj:goal-x", "advances"),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["proj-a.md"]["blocks_objectives"] == [], (
            "node with no blocker must have blocks_objectives=[]"
        )

    def test_rollup_objective_blocked_by_empty_when_no_blocker_reaches_it(self):
        """AC5 complement: objective with no upstream blockers → blocked_by=[]."""
        nodes = [
            _rollup_node("proj-a.md", "project", blocker=None),
            _rollup_node("obj:goal-x", "objective"),
        ]
        edges = [("proj-a.md", "obj:goal-x", "advances")]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["obj:goal-x"]["blocked_by"] == [], (
            "objective reached only by unblocked nodes must have blocked_by=[]"
        )

    def test_rollup_direct_advance_single_hop(self):
        """Blocked node directly advances an objective — distance=1."""
        nodes = [
            _rollup_node("proj-a.md", "project", blocker="disk full"),
            _rollup_node("obj:goal-x", "objective"),
        ]
        edges = [("proj-a.md", "obj:goal-x", "advances")]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["proj-a.md"]["blocks_objectives"] == ["obj:goal-x"]
        assert len(by_id["obj:goal-x"]["blocked_by"]) == 1
        entry = by_id["obj:goal-x"]["blocked_by"][0]
        assert entry["node"] == "proj-a.md"
        assert entry["text"] == "disk full"
        assert entry["distance"] == 1

    def test_rollup_requires_edge_propagates_blocker(self):
        """AC1-shape: blocked prereq node, requires edge into advancer, then advances obj.

        Topology: prereq-node →requires→ advancer →advances→ obj:goal
        prereq-node has blocker; should appear in obj:goal.blocked_by at distance 2.
        """
        nodes = [
            _rollup_node("prereq.md", "project", blocker="blocked!"),
            _rollup_node("advancer.md", "project"),
            _rollup_node("obj:goal", "objective"),
        ]
        # prereq.md → advancer.md via requires (prereq is upstream source)
        # advancer.md → obj:goal via advances
        edges = [
            ("prereq.md", "advancer.md", "requires"),
            ("advancer.md", "obj:goal", "advances"),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert "obj:goal" in by_id["prereq.md"]["blocks_objectives"]
        assert len(by_id["obj:goal"]["blocked_by"]) == 1
        entry = by_id["obj:goal"]["blocked_by"][0]
        assert entry["node"] == "prereq.md"
        assert entry["distance"] == 2

    def test_rollup_cycle_safe_terminates(self):
        """AC4: A requires cycle (a→b, b→a) with b advancing obj:x and a blocked must
        terminate cleanly and report a.blocks_objectives==['obj:x'].
        """
        # Topology:
        #   a →requires→ b  (a is prereq of b)
        #   b →requires→ a  (b is prereq of a — CYCLE)
        #   b →advances→ obj:x
        #   a has blocker
        nodes = [
            _rollup_node("node-a.md", "project", blocker="cycle blocker"),
            _rollup_node("node-b.md", "project"),
            _rollup_node("obj:x", "objective"),
        ]
        edges = [
            ("node-a.md", "node-b.md", "requires"),
            ("node-b.md", "node-a.md", "requires"),
            ("node-b.md", "obj:x", "advances"),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        # Must not hang; must include obj:x
        assert "obj:x" in by_id["node-a.md"]["blocks_objectives"], (
            "cycle-safe rollup must find obj:x reachable from node-a via node-b"
        )
        # node-b (no blocker) → blocks_objectives == []
        assert by_id["node-b.md"]["blocks_objectives"] == [], (
            "node-b has no blocker → blocks_objectives must be []"
        )
        # obj:x.blocked_by must contain node-a but not node-b
        bd_nodes = {e["node"] for e in by_id["obj:x"]["blocked_by"]}
        assert "node-a.md" in bd_nodes
        assert "node-b.md" not in bd_nodes

    def test_rollup_related_edges_ignored(self):
        """Edges of type 'related' must NOT propagate blockers."""
        nodes = [
            _rollup_node("blocked.md", "project", blocker="outage"),
            _rollup_node("other.md", "project"),
            _rollup_node("obj:target", "objective"),
        ]
        # blocked.md is related to other.md; other.md advances the objective
        # but 'related' should not carry the blocker forward
        edges = [
            ("blocked.md", "other.md", "related"),
            ("other.md", "obj:target", "advances"),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["blocked.md"]["blocks_objectives"] == [], (
            "related edges must not propagate blockers"
        )
        assert by_id["obj:target"]["blocked_by"] == []

    def test_rollup_minimum_distance_via_diamond(self):
        """BFS must record minimum-hop distance when two paths reach the same objective.

        Diamond: blocker→mid1→obj and blocker→mid2→mid1→obj.
        Direct path distance=2 (blocker→mid1→obj) wins over the longer path distance=3.
        """
        nodes = [
            _rollup_node("blocker.md", "project", blocker="broken"),
            _rollup_node("mid1.md", "project"),
            _rollup_node("mid2.md", "project"),
            _rollup_node("obj:diamond", "objective"),
        ]
        edges = [
            ("blocker.md", "mid1.md", "requires"),   # hop 1
            ("mid1.md", "obj:diamond", "advances"),   # hop 2 → min dist 2
            ("blocker.md", "mid2.md", "requires"),    # hop 1
            ("mid2.md", "mid1.md", "requires"),       # hop 2
            # mid1 → obj:diamond already above (hop 3 via this path)
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert "obj:diamond" in by_id["blocker.md"]["blocks_objectives"]
        bd = by_id["obj:diamond"]["blocked_by"]
        assert len(bd) == 1
        assert bd[0]["distance"] == 2, (
            f"minimum distance must be 2 (direct path); got {bd[0]['distance']}"
        )

    def test_rollup_all_nodes_gain_blocks_objectives_key(self):
        """Every node in the input must have blocks_objectives key after the call."""
        nodes = [
            _rollup_node("n1.md", "project", blocker="x"),
            _rollup_node("n2.md", "project"),
            _rollup_node("obj:y", "objective"),
        ]
        edges = [("n1.md", "obj:y", "advances")]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        for n in nodes:
            assert "blocks_objectives" in n, (
                f"node {n['id']!r} missing blocks_objectives key"
            )

    def test_rollup_all_objectives_gain_blocked_by_key(self):
        """Every objective node must have blocked_by key after the call."""
        nodes = [
            _rollup_node("n1.md", "project"),
            _rollup_node("obj:a", "objective"),
            _rollup_node("obj:b", "objective"),
        ]
        edges = [("n1.md", "obj:a", "advances"), ("obj:a", "obj:b", "advances")]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        for n in nodes:
            if n["type"] == "objective":
                assert "blocked_by" in n, (
                    f"objective node {n['id']!r} missing blocked_by key"
                )

    def test_rollup_blocked_by_sorted_by_distance_then_node(self):
        """blocked_by list must be sorted ascending by (distance, node) id."""
        nodes = [
            _rollup_node("alpha.md", "project", blocker="b1"),
            _rollup_node("zeta.md", "project", blocker="b2"),
            _rollup_node("obj:end", "objective"),
        ]
        # alpha is a direct hop=1 advancer; zeta is also hop=1
        edges = [
            ("alpha.md", "obj:end", "advances"),
            ("zeta.md", "obj:end", "advances"),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        bd = by_id["obj:end"]["blocked_by"]
        assert len(bd) == 2
        # Same distance → sort by node id alphabetically: alpha < zeta
        assert bd[0]["node"] == "alpha.md"
        assert bd[1]["node"] == "zeta.md"


# ---------------------------------------------------------------------------
# Integration tests — full build_graph_facts vault fixture (AC1, AC2, AC3)
# ---------------------------------------------------------------------------

class TestRollupBlockersIntegration:
    """Integration: AC1/2/3 tested via build_graph_facts with a minimal vault.

    Vault topology mirrors the real projbeta chain:
        projgamma   →requires→ projbeta  →advances→ obj:objective-b
                                         obj:objective-b →advances→ obj:objective-a
        projalpha →requires→ projbeta  (projbeta requires projalpha)

    Both projgamma and projalpha have blockers.
    Expected:
        projgamma.blocks_objectives ⊇ {obj:objective-b, obj:objective-a}
        projalpha.blocks_objectives ⊇ {obj:objective-b, obj:objective-a}
        obj:objective-a.blocked_by has entries for both, sorted by (distance, node).
    """

    def _write_objectives(self, tmp_path: Path) -> None:
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  objective-a:\n"
            '    label: "Own the app-stack market"\n'
            "    kind: ultimate\n"
            "  objective-b:\n"
            '    label: "Detection Engine GA"\n'
            "    kind: milestone\n"
            '    advances: ["objective-a"]\n'
            "project_advances:\n"
            '  projbeta: ["objective-b"]\n',
            encoding="utf-8",
        )

    def _build_notes(self) -> list[dict]:
        """
        projgamma:     has a blocker, is a prereq of projbeta
        projalpha: has a blocker, is a prereq of projbeta
        projbeta:  advances obj:objective-b (via project_advances in objectives.yaml)
                     requires: projgamma and projalpha (via frontmatter)
        """
        return [
            _make_note(
                "02-projects/3.0-work/projgamma.md", "projgamma",
                extra_fm=(
                    "blockers:\n"
                    '  - text: "projgamma offline"\n'
                    "    severity: high\n"
                ),
            ),
            _make_note(
                "02-projects/3.0-work/projalpha.md", "projalpha",
                extra_fm=(
                    "blockers:\n"
                    '  - text: "projalpha outage"\n'
                    "    severity: high\n"
                ),
            ),
            _make_note(
                "02-projects/1.0-dev/projbeta.md", "projbeta",
                extra_fm=(
                    'requires: ["[[projgamma]]", "[[projalpha]]"]\n'
                ),
            ),
        ]

    def _run(self, tmp_path: Path):
        self._write_objectives(tmp_path)
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        return data

    def test_rollup_blocker_text_extracted(self, tmp_path):
        """Sanity: projgamma and projalpha nodes must have non-None blocker text.

        If this fails, the fixture's block-list form is not being parsed correctly —
        fix the fixture before diagnosing rollup logic.
        """
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        asecure_id = "02-projects/3.0-work/projalpha.md"
        assert by_id[projgamma_id]["blocker"] is not None, (
            "projgamma must have a non-None blocker; check block-list fixture format"
        )
        assert by_id[asecure_id]["blocker"] is not None, (
            "projalpha must have a non-None blocker; check block-list fixture format"
        )

    def test_rollup_projgamma_reaches_objective_b(self, tmp_path):
        """AC1 (partial): projgamma.blocks_objectives contains obj:objective-b."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        bobj = by_id[projgamma_id].get("blocks_objectives", [])
        assert "obj:objective-b" in bobj, (
            f"projgamma must reach obj:objective-b; got blocks_objectives={bobj}"
        )

    def test_rollup_projgamma_reaches_objective_a(self, tmp_path):
        """AC1: projgamma.blocks_objectives contains obj:objective-a (transitive)."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        bobj = by_id[projgamma_id].get("blocks_objectives", [])
        assert "obj:objective-a" in bobj, (
            f"projgamma must transitively reach obj:objective-a; got {bobj}"
        )

    def test_rollup_projalpha_reaches_objective_a(self, tmp_path):
        """AC2: projalpha.blocks_objectives contains obj:objective-a."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        asecure_id = "02-projects/3.0-work/projalpha.md"
        bobj = by_id[asecure_id].get("blocks_objectives", [])
        assert "obj:objective-a" in bobj, (
            f"projalpha must transitively reach obj:objective-a; got {bobj}"
        )

    def test_rollup_obj_objective_a_blocked_by_both(self, tmp_path):
        """AC3: obj:objective-a.blocked_by lists both projgamma and projalpha."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        ult_node = by_id.get("obj:objective-a", {})
        bd = ult_node.get("blocked_by", [])
        bd_node_ids = {e["node"] for e in bd}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        asecure_id = "02-projects/3.0-work/projalpha.md"
        assert projgamma_id in bd_node_ids, (
            f"obj:objective-a.blocked_by must include projgamma; got {bd_node_ids}"
        )
        assert asecure_id in bd_node_ids, (
            f"obj:objective-a.blocked_by must include projalpha; got {bd_node_ids}"
        )

    def test_rollup_blocked_by_sorted_ascending_distance(self, tmp_path):
        """AC3: blocked_by must be sorted by (distance, node) ascending."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        ult_node = by_id.get("obj:objective-a", {})
        bd = ult_node.get("blocked_by", [])
        for i in range(len(bd) - 1):
            a, b = bd[i], bd[i + 1]
            assert (a["distance"], a["node"]) <= (b["distance"], b["node"]), (
                f"blocked_by must be sorted by (distance, node); "
                f"entry {i}={(a['distance'], a['node'])} > entry {i+1}={(b['distance'], b['node'])}"
            )

    def test_rollup_blocks_objectives_is_sorted(self, tmp_path):
        """blocks_objectives list must be sorted."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        bobj = by_id[projgamma_id].get("blocks_objectives", [])
        assert bobj == sorted(bobj), (
            f"blocks_objectives must be sorted; got {bobj}"
        )

    def test_rollup_projbeta_no_blocker_empty_blocks_objectives(self, tmp_path):
        """projbeta has no blocker → blocks_objectives=[]."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        avv_id = "02-projects/1.0-dev/projbeta.md"
        bobj = by_id[avv_id].get("blocks_objectives", [])
        assert bobj == [], (
            f"projbeta has no blocker → blocks_objectives must be []; got {bobj}"
        )

    def test_rollup_fields_present_in_graph_json_for_all_nodes(self, tmp_path):
        """Every node in graph.json must carry blocks_objectives; every objective carries blocked_by."""
        data = self._run(tmp_path)
        for n in data["nodes"]:
            assert "blocks_objectives" in n, (
                f"node {n['id']!r} missing blocks_objectives in graph.json"
            )
            if n.get("type") == "objective":
                assert "blocked_by" in n, (
                    f"objective node {n['id']!r} missing blocked_by in graph.json"
                )

    def test_rollup_obj_objective_a_blocked_by_exact_distances(self, tmp_path):
        """FIX 2: projgamma and projalpha must both appear with distance==3 in
        obj:objective-a.blocked_by.

        Hop count trace (requires + advances chain only):
          projgamma     →requires→ projbeta (1)
                      →advances→ obj:objective-b (2)
                      →advances→ obj:objective-a (3)
          projalpha →requires→ projbeta (1)
                       →advances→ obj:objective-b (2)
                       →advances→ obj:objective-a (3)
        """
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        ult_node = by_id.get("obj:objective-a", {})
        bd = ult_node.get("blocked_by", [])
        dist_by_node = {e["node"]: e["distance"] for e in bd}
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        asecure_id = "02-projects/3.0-work/projalpha.md"
        assert dist_by_node.get(projgamma_id) == 3, (
            f"projgamma must be distance=3 from obj:objective-a; "
            f"got dist_by_node={dist_by_node}"
        )
        assert dist_by_node.get(asecure_id) == 3, (
            f"projalpha must be distance=3 from obj:objective-a; "
            f"got dist_by_node={dist_by_node}"
        )


# ---------------------------------------------------------------------------
# FIX 1 — excluded edge types must NOT propagate blockers through rollup
# ---------------------------------------------------------------------------

class TestRollupExcludedEdgeTypes:
    """_ROLLUP_EDGE_TYPES must stay exactly {requires, advances}.

    Each test puts a blocked source node and an objective together, connected
    only by a SINGLE excluded edge type.  The source MUST reach the objective
    via that type if the type were included — but since it is excluded, the
    rollup must produce blocks_objectives==[] and blocked_by==[].

    This guards _ROLLUP_EDGE_TYPES against accidental expansion.
    """

    @pytest.mark.parametrize("excluded_edge_type", [
        "partof",
        "supersedes",
        "gates",
        "blocked",
        "related",
    ])
    def test_rollup_excluded_edge_type_does_not_propagate(self, excluded_edge_type):
        """A blocked node that can ONLY reach an objective via an excluded edge type
        must have blocks_objectives==[] and the objective must have blocked_by==[].
        """
        nodes = [
            _rollup_node("blocked-src.md", "project", blocker="critical outage"),
            _rollup_node("obj:target", "objective"),
        ]
        # The only edge from blocked-src.md → obj:target is of the excluded type.
        edges = [
            ("blocked-src.md", "obj:target", excluded_edge_type),
        ]
        KB_GRAPH._rollup_blockers_to_objectives(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["blocked-src.md"]["blocks_objectives"] == [], (
            f"edge type {excluded_edge_type!r} must NOT propagate blockers; "
            f"blocked-src.md.blocks_objectives should be [] but got "
            f"{by_id['blocked-src.md']['blocks_objectives']}"
        )
        assert by_id["obj:target"]["blocked_by"] == [], (
            f"edge type {excluded_edge_type!r} must NOT propagate to objective's blocked_by; "
            f"obj:target.blocked_by should be [] but got "
            f"{by_id['obj:target']['blocked_by']}"
        )


# ---------------------------------------------------------------------------
# TASK 8 — _critical_path_to_objective
# ---------------------------------------------------------------------------

def _make_ultimate_node(obj_id: str, blocked_by: list) -> dict:
    """Build a minimal ultimate-objective dict as _rollup_blockers_to_objectives produces."""
    return {
        "id": obj_id,
        "type": "objective",
        "kind": "ultimate",
        "blocker": None,
        "blocked_by": blocked_by,
        "blocks_objectives": [],
    }


def _make_milestone_node(obj_id: str) -> dict:
    return {
        "id": obj_id,
        "type": "objective",
        "kind": "milestone",
        "blocker": None,
        "blocked_by": [],
        "blocks_objectives": [],
    }


def _make_blocker_node(node_id: str, blocks_objectives: list) -> dict:
    return {
        "id": node_id,
        "type": "project",
        "blocker": "some blocker text",
        "blocks_objectives": blocks_objectives,
    }


class TestCriticalPathToObjective:
    """Unit tests for _critical_path_to_objective(nodes).

    Pre-condition: nodes already have blocks_objectives (on all nodes) and
    blocked_by (on ultimate objectives), as mutated by _rollup_blockers_to_objectives.
    _critical_path_to_objective is called directly — no full vault overhead.
    """

    def test_critical_high_impeded_count_wins(self):
        """Blocker A impedes 3 objectives, blocker B impedes 1 → A is chosen; score==3."""
        # Blocker A blocks 3 objectives; blocker B blocks only 1.
        blocker_a = _make_blocker_node("blocker-a.md", ["obj:x", "obj:y", "obj:z"])
        blocker_b = _make_blocker_node("blocker-b.md", ["obj:x"])
        ultimate = _make_ultimate_node(
            "obj:x",
            blocked_by=[
                {"node": "blocker-a.md", "text": "Text A", "distance": 1},
                {"node": "blocker-b.md", "text": "Text B", "distance": 1},
            ],
        )
        nodes = [blocker_a, blocker_b, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        cb = ultimate.get("critical_blocker")
        assert cb is not None, "ultimate objective with blockers must have critical_blocker != null"
        assert cb["node"] == "blocker-a.md", (
            f"blocker-a (impedes 3) must beat blocker-b (impedes 1); got node={cb['node']!r}"
        )
        assert cb["score"] == 3, (
            f"score must equal the impeded-objective count of the chosen blocker (3); got {cb['score']}"
        )
        assert cb["text"] == "Text A", f"text must come from the chosen blocker's blocked_by entry; got {cb['text']!r}"

    def test_critical_no_blockers_yields_null(self):
        """An ultimate objective with no blocked_by entries → critical_blocker is None."""
        ultimate = _make_ultimate_node("obj:lonely", blocked_by=[])
        nodes = [ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        assert ultimate.get("critical_blocker") is None, (
            "ultimate objective with no blockers must have critical_blocker==None"
        )

    def test_critical_tie_same_impeded_same_distance_smaller_id_wins(self):
        """Tie: equal impeded_count + equal distance → lexicographically smaller node id wins."""
        # Both blockers impede 2 objectives each, both at distance 1.
        blocker_alpha = _make_blocker_node("blocker-alpha.md", ["obj:x", "obj:y"])
        blocker_zeta = _make_blocker_node("blocker-zeta.md", ["obj:x", "obj:q"])
        ultimate = _make_ultimate_node(
            "obj:x",
            blocked_by=[
                {"node": "blocker-alpha.md", "text": "Alpha text", "distance": 1},
                {"node": "blocker-zeta.md", "text": "Zeta text", "distance": 1},
            ],
        )
        nodes = [blocker_alpha, blocker_zeta, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        cb = ultimate.get("critical_blocker")
        assert cb is not None
        assert cb["node"] == "blocker-alpha.md", (
            "blocker-alpha < blocker-zeta lexicographically → alpha must win the tie; "
            f"got {cb['node']!r}"
        )

    def test_critical_tie_same_impeded_higher_distance_wins(self):
        """Tie on impeded_count, different distance → higher distance (more foundational) wins."""
        # Both impede 2 objectives; blocker-near is distance 1, blocker-far is distance 3.
        blocker_near = _make_blocker_node("blocker-near.md", ["obj:x", "obj:y"])
        blocker_far = _make_blocker_node("blocker-far.md", ["obj:x", "obj:z"])
        ultimate = _make_ultimate_node(
            "obj:x",
            blocked_by=[
                {"node": "blocker-near.md", "text": "Near text", "distance": 1},
                {"node": "blocker-far.md", "text": "Far text", "distance": 3},
            ],
        )
        nodes = [blocker_near, blocker_far, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        cb = ultimate.get("critical_blocker")
        assert cb is not None
        assert cb["node"] == "blocker-far.md", (
            "higher distance (more foundational) must win when impeded_count is equal; "
            f"got {cb['node']!r}"
        )

    def test_critical_only_ultimate_objectives_gain_field(self):
        """Milestone objectives and non-objective nodes must NOT get critical_blocker."""
        blocker = _make_blocker_node("some-blocker.md", ["obj:ult"])
        milestone = _make_milestone_node("obj:ms")
        # Give milestone a blocked_by too (should be ignored)
        milestone["blocked_by"] = [{"node": "some-blocker.md", "text": "t", "distance": 1}]
        ultimate = _make_ultimate_node(
            "obj:ult",
            blocked_by=[{"node": "some-blocker.md", "text": "t", "distance": 1}],
        )
        project_node = _make_blocker_node("proj.md", [])
        nodes = [blocker, milestone, ultimate, project_node]
        KB_GRAPH._critical_path_to_objective(nodes)
        assert "critical_blocker" not in milestone, (
            "milestone objectives must not gain critical_blocker"
        )
        assert "critical_blocker" not in project_node, (
            "project nodes must not gain critical_blocker"
        )
        assert "critical_blocker" in ultimate, (
            "ultimate objective must gain critical_blocker"
        )

    def test_critical_single_blocker_is_chosen(self):
        """With exactly one blocker, it must be chosen regardless of its impeded count."""
        blocker = _make_blocker_node("solo-blocker.md", ["obj:solo-ult"])
        ultimate = _make_ultimate_node(
            "obj:solo-ult",
            blocked_by=[{"node": "solo-blocker.md", "text": "Only blocker", "distance": 2}],
        )
        nodes = [blocker, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        cb = ultimate.get("critical_blocker")
        assert cb is not None
        assert cb["node"] == "solo-blocker.md"
        assert cb["score"] == 1  # impedes only this objective itself
        assert cb["text"] == "Only blocker"

    def test_critical_idempotent_double_call(self):
        """Calling _critical_path_to_objective twice on the same nodes is idempotent."""
        blocker = _make_blocker_node("b.md", ["obj:u"])
        ultimate = _make_ultimate_node(
            "obj:u",
            blocked_by=[{"node": "b.md", "text": "block", "distance": 1}],
        )
        nodes = [blocker, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        first = dict(ultimate["critical_blocker"])
        KB_GRAPH._critical_path_to_objective(nodes)
        assert ultimate["critical_blocker"] == first, "double-call must be idempotent"

    def test_critical_primary_beats_secondary_opposite_directions(self):
        """FIX 1 — PRIMARY criterion (impeded_count) must beat SECONDARY (distance)
        when they point in opposite directions.

        Blocker P impedes 3 objectives at distance 1.
        Blocker Q impedes 2 objectives at distance 5.
        Both are in the ultimate's blocked_by.
        P has the higher PRIMARY (impeded_count=3) but the lower SECONDARY (distance=1).
        Q has the lower PRIMARY (impeded_count=2) but the higher SECONDARY (distance=5).
        PRIMARY must win → P must be chosen, score == 3.

        A regression swapping the ranking tuple to (-distance, -impeded, node) would
        choose Q instead; this test locks the criterion order.
        """
        blocker_p = _make_blocker_node("blocker-p.md", ["obj:a", "obj:b", "obj:c"])  # impedes 3
        blocker_q = _make_blocker_node("blocker-q.md", ["obj:a", "obj:d"])            # impedes 2
        ultimate = _make_ultimate_node(
            "obj:a",
            blocked_by=[
                {"node": "blocker-p.md", "text": "P text", "distance": 1},
                {"node": "blocker-q.md", "text": "Q text", "distance": 5},
            ],
        )
        nodes = [blocker_p, blocker_q, ultimate]
        KB_GRAPH._critical_path_to_objective(nodes)
        cb = ultimate.get("critical_blocker")
        assert cb is not None, "critical_blocker must not be None when blockers exist"
        assert cb["node"] == "blocker-p.md", (
            f"PRIMARY (impeded_count=3) must beat SECONDARY (distance=5 on Q); "
            f"expected blocker-p.md but got {cb['node']!r}"
        )
        assert cb["score"] == 3, (
            f"score must equal the chosen blocker's impeded_count (3); got {cb['score']}"
        )


class TestCriticalPathIntegration:
    """Integration: critical_blocker lands in graph.json for the ultimate objective.

    Reuses the two-blocker topology from TestRollupBlockersIntegration:
        projgamma     →requires→ projbeta →advances→ obj:objective-b
                                          →advances→ obj:objective-a
        projalpha →requires→ projbeta

    Both projgamma and projalpha have blockers. They reach both objectives, each
    at distance 3 from obj:objective-a. They each impede 2 objectives.
    Tie on (impeded_count=2, distance=3) → lexicographic tiebreak.
    projalpha < projgamma alphabetically → projalpha wins.
    """

    def _write_objectives(self, tmp_path: Path) -> None:
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  objective-a:\n"
            '    label: "Own the app-stack market"\n'
            "    kind: ultimate\n"
            "  objective-b:\n"
            '    label: "Detection Engine GA"\n'
            "    kind: milestone\n"
            '    advances: ["objective-a"]\n'
            "project_advances:\n"
            '  projbeta: ["objective-b"]\n',
            encoding="utf-8",
        )

    def _build_notes(self) -> list[dict]:
        return [
            _make_note(
                "02-projects/3.0-work/projgamma.md", "projgamma",
                extra_fm=(
                    "blockers:\n"
                    '  - text: "projgamma offline"\n'
                    "    severity: high\n"
                ),
            ),
            _make_note(
                "02-projects/3.0-work/projalpha.md", "projalpha",
                extra_fm=(
                    "blockers:\n"
                    '  - text: "projalpha outage"\n'
                    "    severity: high\n"
                ),
            ),
            _make_note(
                "02-projects/1.0-dev/projbeta.md", "projbeta",
                extra_fm='requires: ["[[projgamma]]", "[[projalpha]]"]\n',
            ),
        ]

    def _run(self, tmp_path: Path):
        self._write_objectives(tmp_path)
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        return data

    def test_critical_blocker_field_in_graph_json(self, tmp_path):
        """Ultimate objective node in graph.json must have 'critical_blocker' key."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        ult = by_id.get("obj:objective-a", {})
        assert "critical_blocker" in ult, (
            "obj:objective-a must carry critical_blocker in graph.json"
        )

    def test_critical_blocker_not_none(self, tmp_path):
        """With two blockers present, critical_blocker must not be null."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        cb = by_id["obj:objective-a"].get("critical_blocker")
        assert cb is not None, "critical_blocker must not be null when blockers exist"

    def test_critical_blocker_shape(self, tmp_path):
        """critical_blocker must have node, text, score keys with correct types."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        cb = by_id["obj:objective-a"]["critical_blocker"]
        assert isinstance(cb["node"], str), "critical_blocker.node must be a str"
        assert isinstance(cb["text"], str), "critical_blocker.text must be a str"
        assert isinstance(cb["score"], int), "critical_blocker.score must be an int"

    def test_critical_blocker_tiebreak_lexicographic(self, tmp_path):
        """Tie on (impeded_count, distance) → lex-smaller node id wins (projalpha < projgamma)."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        cb = by_id["obj:objective-a"]["critical_blocker"]
        asecure_id = "02-projects/3.0-work/projalpha.md"
        projgamma_id = "02-projects/3.0-work/projgamma.md"
        assert cb["node"] in (asecure_id, projgamma_id), (
            f"critical_blocker.node must be one of the two known blockers; got {cb['node']!r}"
        )
        assert cb["node"] == asecure_id, (
            f"projalpha < projgamma alphabetically → projalpha must win the tiebreak; "
            f"got {cb['node']!r}"
        )

    def test_critical_blocker_score_equals_impeded_count(self, tmp_path):
        """score == len(blocks_objectives) of the chosen blocker."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        cb = by_id["obj:objective-a"]["critical_blocker"]
        chosen_node = by_id[cb["node"]]
        expected_score = len(chosen_node.get("blocks_objectives", []))
        assert cb["score"] == expected_score, (
            f"score must equal len(blocks_objectives) of chosen blocker; "
            f"got score={cb['score']}, expected={expected_score}"
        )

    def test_critical_blocker_milestone_has_no_field(self, tmp_path):
        """Milestone objective must not gain critical_blocker in graph.json."""
        data = self._run(tmp_path)
        by_id = {n["id"]: n for n in data["nodes"]}
        ms = by_id.get("obj:objective-b", {})
        assert "critical_blocker" not in ms, (
            "milestone objective must not carry critical_blocker in graph.json"
        )

    def test_critical_null_emitted_when_no_upstream_blockers(self, tmp_path):
        """FIX 2 — integration null-emission test.

        An ultimate objective with NO upstream blocked nodes must emit
        critical_blocker: null (Python None) end-to-end through graph.json.

        Topology: one ultimate objective with no project notes reaching it
        → blocked_by=[] → critical_blocker emitted as null in JSON.
        """
        meta = tmp_path / "00-meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "objectives.yaml").write_text(
            "objectives:\n"
            "  solo-ultimate:\n"
            '    label: "Solo ultimate"\n'
            "    kind: ultimate\n",
            encoding="utf-8",
        )
        conn = _setup_db([])
        KB_GRAPH.build_graph_facts(conn, [], tmp_path)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        nodes_by_id = {n["id"]: n for n in data["nodes"]}
        ult = nodes_by_id.get("obj:solo-ultimate")
        assert ult is not None, "obj:solo-ultimate must appear in graph.json"
        assert "critical_blocker" in ult, (
            "ultimate objective must always carry the critical_blocker key in graph.json"
        )
        assert ult["critical_blocker"] is None, (
            f"ultimate objective with no upstream blockers must have critical_blocker==null "
            f"in graph.json; got {ult['critical_blocker']!r}"
        )


# ---------------------------------------------------------------------------
# Staleness-based freshness tests (replaces the vacuous updated-based metric)
# ---------------------------------------------------------------------------

class TestFreshness:
    """lineage-quality.json project_freshness derived from injected staleness map.

    All tests inject a pre-computed {name: state} stub via the `staleness`
    parameter — no live git, no real vault needed.
    """

    # Three project notes (paths encode the vault-relative form used as node id).
    # Stems: "proj-alpha", "proj-beta", "proj-gamma".
    # One non-project note (type: adr) must be excluded from freshness counts.
    _PROJ_PATHS = [
        "02-projects/1.0-dev/proj-alpha.md",
        "02-projects/1.0-dev/proj-beta.md",
        "02-projects/1.0-dev/proj-gamma.md",
    ]

    def _build_notes(self) -> list[dict]:
        notes = [
            _make_note(self._PROJ_PATHS[0], "proj-alpha", note_type="project"),
            _make_note(self._PROJ_PATHS[1], "proj-beta",  note_type="project"),
            _make_note(self._PROJ_PATHS[2], "proj-gamma", note_type="project"),
            _make_note("docs/some-adr.md",  "some-adr",   note_type="adr"),
        ]
        return notes

    def _run(self, tmp_path, staleness_map):
        notes = self._build_notes()
        conn = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path, staleness=staleness_map)
        lq_path = tmp_path / "00-meta" / "lineage-quality.json"
        with open(lq_path, encoding="utf-8") as f:
            return json.load(f)

    def test_lineage_quality_emitted(self, tmp_path):
        """lineage-quality.json must be written alongside graph.json."""
        data = self._run(tmp_path, {"proj-alpha": "fresh", "proj-beta": "stale", "proj-gamma": "fresh"})
        assert "summary" in data
        assert "project_freshness" in data["summary"]

    def test_injected_map_drives_counts(self, tmp_path):
        """Exact counts from a known injected map must match summary.project_freshness."""
        stub = {
            "proj-alpha": "fresh",
            "proj-beta":  "stale",
            "proj-gamma": "very_stale",
        }
        data = self._run(tmp_path, stub)
        pf = data["summary"]["project_freshness"]
        assert pf == {"fresh": 1, "stale": 1, "very_stale": 1, "unknown": 0}, (
            f"project_freshness mismatch; got {pf}"
        )

    def test_absent_project_yields_unknown(self, tmp_path):
        """A project whose stem has no entry in the staleness map must be counted as unknown."""
        # Only proj-alpha and proj-beta are in the stub; proj-gamma is absent.
        stub = {
            "proj-alpha": "fresh",
            "proj-beta":  "fresh",
        }
        data = self._run(tmp_path, stub)
        pf = data["summary"]["project_freshness"]
        assert pf["unknown"] == 1, (
            f"proj-gamma has no staleness entry → must count as unknown; got {pf}"
        )

    def test_changing_state_changes_counts(self, tmp_path):
        """Flipping one entry's state must change the output counts (non-vacuous probe)."""
        stub_a = {"proj-alpha": "fresh",  "proj-beta": "fresh",  "proj-gamma": "fresh"}
        stub_b = {"proj-alpha": "stale",  "proj-beta": "fresh",  "proj-gamma": "fresh"}

        data_a = self._run(tmp_path, stub_a)
        # Need a fresh tmp_path for second run — reuse by giving a sub-dir.
        sub = tmp_path / "run_b"
        sub.mkdir()
        notes = self._build_notes()
        conn2 = _setup_db(notes)
        KB_GRAPH.build_graph_facts(conn2, notes, sub, staleness=stub_b)
        with open(sub / "00-meta" / "lineage-quality.json", encoding="utf-8") as f:
            data_b = json.load(f)

        pf_a = data_a["summary"]["project_freshness"]
        pf_b = data_b["summary"]["project_freshness"]
        assert pf_a != pf_b, (
            "Changing one project's state from fresh→stale must change project_freshness counts; "
            f"both returned {pf_a}"
        )
        assert pf_a == {"fresh": 3, "stale": 0, "very_stale": 0, "unknown": 0}
        assert pf_b == {"fresh": 2, "stale": 1, "very_stale": 0, "unknown": 0}

    def test_non_project_nodes_excluded(self, tmp_path):
        """ADR/gate/other typed nodes must not appear in project_freshness counts."""
        # Mark all 3 projects fresh; total count must be exactly 3.
        stub = {"proj-alpha": "fresh", "proj-beta": "fresh", "proj-gamma": "fresh"}
        data = self._run(tmp_path, stub)
        pf = data["summary"]["project_freshness"]
        total = sum(pf.values())
        assert total == 3, (
            f"only 3 project nodes; non-project nodes must be excluded from counts; got {pf}"
        )

    def test_stale_nodes_list_excludes_fresh(self, tmp_path):
        """stale_nodes must contain stale/very_stale/unknown nodes, not fresh ones."""
        stub = {
            "proj-alpha": "fresh",
            "proj-beta":  "stale",
            "proj-gamma": "very_stale",
        }
        data = self._run(tmp_path, stub)
        stale_ids = {n["id"] for n in data["stale_nodes"]}
        assert self._PROJ_PATHS[0] not in stale_ids, (
            "proj-alpha is fresh → must not appear in stale_nodes"
        )
        assert self._PROJ_PATHS[1] in stale_ids, (
            "proj-beta is stale → must appear in stale_nodes"
        )
        assert self._PROJ_PATHS[2] in stale_ids, (
            "proj-gamma is very_stale → must appear in stale_nodes"
        )

    def test_stale_nodes_have_freshness_field(self, tmp_path):
        """Each stale_nodes entry must carry an 'id', 'label', and 'freshness' field."""
        stub = {"proj-alpha": "stale", "proj-beta": "fresh", "proj-gamma": "unknown"}
        data = self._run(tmp_path, stub)
        for entry in data["stale_nodes"]:
            assert "id" in entry, f"missing 'id' in stale_node: {entry}"
            assert "label" in entry, f"missing 'label' in stale_node: {entry}"
            assert "freshness" in entry, f"missing 'freshness' in stale_node: {entry}"
            assert "updated" not in entry, (
                f"stale_node must not carry vacuous 'updated' field: {entry}"
            )

    def test_graph_json_nodes_have_no_freshness_key(self, tmp_path):
        """Freshness must stay out of graph.json — it's time-varying and only in lineage-quality."""
        stub = {"proj-alpha": "stale", "proj-beta": "fresh", "proj-gamma": "very_stale"}
        self._run(tmp_path, stub)
        with open(tmp_path / "00-meta" / "graph.json", encoding="utf-8") as f:
            gj = json.load(f)
        for node in gj["nodes"]:
            assert "freshness" not in node, (
                f"freshness must not appear in graph.json node {node['id']!r}"
            )
            assert "staleness" not in node, (
                f"staleness must not appear in graph.json node {node['id']!r}"
            )

    def test_empty_staleness_map_all_unknown(self, tmp_path):
        """When the staleness map is empty, every project node defaults to unknown."""
        data = self._run(tmp_path, {})
        pf = data["summary"]["project_freshness"]
        assert pf["unknown"] == 3, (
            f"empty staleness map → all 3 projects must be unknown; got {pf}"
        )
        assert pf["fresh"] == 0
        assert pf["stale"] == 0
        assert pf["very_stale"] == 0

    def test_unrecognized_state_clamped_to_unknown(self, tmp_path):
        """An unrecognized state string in the staleness map must be clamped to 'unknown'.

        Non-vacuous: the project note stem 'proj-alpha' must match the map key so the
        clamp code-path is actually exercised.  Removing the clamp (``if _fr not in
        freshness_counts: _fr = "unknown"``) causes freshness_counts["GARBAGE"] to raise
        KeyError, turning this test RED.
        """
        # Only proj-alpha has a stub entry with a garbage state; the other two
        # have no entry and will default to "unknown" via .get(_stem, "unknown").
        stub = {"proj-alpha": "GARBAGE"}
        data = self._run(tmp_path, stub)
        pf = data["summary"]["project_freshness"]
        # proj-alpha "GARBAGE" → clamped to unknown; proj-beta + proj-gamma → unknown
        assert pf["unknown"] == 3, (
            f"unrecognized state 'GARBAGE' must be clamped to unknown; got {pf}"
        )
        assert pf["fresh"] == 0
        assert pf["stale"] == 0
        assert pf["very_stale"] == 0

    def test_no_staleness_kwarg_smoke(self, tmp_path):
        """build_graph_facts without staleness= must not raise and must emit project_freshness.

        Covers the live path: staleness=None triggers _load_staleness() which tries to
        load kb-staleness.py from the skill directory.  The vault root is tmp_path with
        no 02-projects/ subdirectory, so compute() returns {} (no cards to scan) and
        every project node falls back to 'unknown'.

        This test verifies that _load_staleness() either succeeds (compute returns {})
        or fails gracefully (returns None → {}), and that the rest of the freshness
        machinery handles that empty map correctly.
        """
        # One project note so freshness_counts is exercised (not just all-zero).
        notes = [_make_note("02-projects/1.0-dev/proj-alpha.md", "proj-alpha", note_type="project")]
        conn = _setup_db(notes)
        # No staleness= kwarg — live path.  No 02-projects/ in tmp_path so compute()
        # returns {} (or load fails gracefully); proj-alpha has no matching entry.
        KB_GRAPH.build_graph_facts(conn, notes, tmp_path)

        lq_path = tmp_path / "00-meta" / "lineage-quality.json"
        assert lq_path.exists(), "lineage-quality.json must be emitted even without staleness="
        with open(lq_path, encoding="utf-8") as f:
            data = json.load(f)

        assert "summary" in data, "lineage-quality.json must have a 'summary' key"
        assert "project_freshness" in data["summary"], (
            "'project_freshness' must be present in summary"
        )
        pf = data["summary"]["project_freshness"]
        # All four canonical keys must be present.
        for key in ("fresh", "stale", "very_stale", "unknown"):
            assert key in pf, f"project_freshness missing key {key!r}"
        # proj-alpha has no entry in the empty computed map → must land in unknown.
        assert pf["unknown"] == 1, (
            f"proj-alpha with no staleness entry must be counted as unknown; got {pf}"
        )
        assert pf["fresh"] == 0
        assert pf["stale"] == 0
        assert pf["very_stale"] == 0
