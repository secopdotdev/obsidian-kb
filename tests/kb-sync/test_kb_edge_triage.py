"""Tests for kb-edge-triage.py — edge classification: critical vs auto_accepted."""
import importlib.util
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location(
        "kb_edge_triage", Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-edge-triage.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


GRAPH = {
    "nodes": [
        {"id": "a", "topo_rank": 0, "blocks_objectives": []},
        {"id": "b", "topo_rank": 1, "blocks_objectives": []},
        {"id": "c", "topo_rank": 2, "blocks_objectives": ["obj:x"]},
    ],
    "edges": [
        {"from": "a", "to": "b", "type": "requires"},
        {"from": "b", "to": "c", "type": "requires"},
    ],
}
LINEAGE = {"a": {"advances": "career"}, "b": {"advances": "career"}, "c": {"advances": "home"}}


def test_cycle_is_critical():
    m = _load()
    out = m.classify_edges([{"from": "c", "to": "a", "type": "requires"}], GRAPH, LINEAGE)
    assert out["critical"] and out["critical"][0]["reason"] == "cycle"


def test_cross_lane_is_critical():
    m = _load()
    out = m.classify_edges([{"from": "a", "to": "c", "type": "requires"}], GRAPH, LINEAGE)
    assert out["critical"] and out["critical"][0]["reason"] == "cross-lane"


def test_duplicate_is_critical():
    m = _load()
    out = m.classify_edges([{"from": "a", "to": "b", "type": "requires"}], GRAPH, LINEAGE)
    assert out["critical"][0]["reason"] == "conflict-dupe"


def test_low_impact_auto_accepted():
    m = _load()
    g = {"nodes": GRAPH["nodes"] + [{"id": "d", "topo_rank": 0, "blocks_objectives": []}],
         "edges": GRAPH["edges"]}
    lin = dict(LINEAGE); lin["d"] = {"advances": "career"}
    out = m.classify_edges([{"from": "d", "to": "a", "type": "requires"}], g, lin)
    assert out["auto_accepted"] and not out["critical"]


def test_cross_lane_isolated_no_fallback():
    # p and q are cross-lane (career vs home) and must NOT trigger high-leverage.
    # Graph: nodes a(rank=0), b(rank=1), c(rank=2), p(rank=0), q(rank=0).
    # ranks sorted = [0,0,0,1,2]; int(5*0.75)=3; thresh=ranks[3]=1.
    # p/q have topo_rank=0 < 1 and blocks_objectives=[] → high-leverage cannot fire.
    # Cross-lane is therefore the sole possible reason — no fallback can mask a failure.
    m = _load()
    g = {
        "nodes": [
            {"id": "a", "topo_rank": 0, "blocks_objectives": []},
            {"id": "b", "topo_rank": 1, "blocks_objectives": []},
            {"id": "c", "topo_rank": 2, "blocks_objectives": []},
            {"id": "p", "topo_rank": 0, "blocks_objectives": []},
            {"id": "q", "topo_rank": 0, "blocks_objectives": []},
        ],
        "edges": [{"from": "a", "to": "b", "type": "requires"},
                  {"from": "b", "to": "c", "type": "requires"}],
    }
    lin = {"p": {"advances": "career"}, "q": {"advances": "home"}}
    out = m.classify_edges([{"from": "p", "to": "q", "type": "requires"}], g, lin)
    assert out["critical"] and out["critical"][0]["reason"] == "cross-lane"
    assert not out["auto_accepted"]


def test_high_leverage_blocks_objectives():
    m = _load()
    g = {
        "nodes": [
            {"id": "a", "topo_rank": 0, "blocks_objectives": []},
            {"id": "b", "topo_rank": 0, "blocks_objectives": ["obj:x"]},
        ],
        "edges": [],
    }
    # same lane (no cross-lane), not a dupe, no cycle -> only blocks_objectives can fire
    lin = {"a": {"advances": "career"}, "b": {"advances": "career"}}
    out = m.classify_edges([{"from": "a", "to": "b", "type": "requires"}], g, lin)
    assert out["critical"] and out["critical"][0]["reason"] == "high-leverage"


def test_high_leverage_topo_rank():
    m = _load()
    # ranks [0,0,0,1,2] -> thresh = ranks[int(5*0.75)] = ranks[3] = 1; node c (rank 2) >= 1
    g = {
        "nodes": [
            {"id": "a", "topo_rank": 0, "blocks_objectives": []},
            {"id": "b", "topo_rank": 1, "blocks_objectives": []},
            {"id": "c", "topo_rank": 2, "blocks_objectives": []},
            {"id": "p", "topo_rank": 0, "blocks_objectives": []},
            {"id": "q", "topo_rank": 0, "blocks_objectives": []},
        ],
        "edges": [{"from": "a", "to": "b", "type": "requires"},
                  {"from": "b", "to": "c", "type": "requires"}],
    }
    # same lane so cross-lane can't fire; edge q->c (c rank 2 >= thresh 1) -> high-leverage
    lin = {"q": {"advances": "career"}, "c": {"advances": "career"}}
    out = m.classify_edges([{"from": "q", "to": "c", "type": "requires"}], g, lin)
    assert out["critical"] and out["critical"][0]["reason"] == "high-leverage"


def test_unknown_lane_does_not_crash_or_drop():
    """Unknown/absent lane for src or dst must not raise an exception and must not
    silently drop the edge — it is still classified in exactly one bucket.

    Cross-lane check: lineage.get(src, {}).get("advances") returns None when the
    project is absent from lineage → the ``ls and ld and ls != ld`` guard short-circuits
    and cross-lane does NOT fire.  The edge is classified on other grounds (or auto).

    Note: with only 2 zero-rank nodes, thresh = ranks[int(2*0.75)] = ranks[1] = 0,
    so topo_rank(0) >= 0 is True → the edge is flagged high-leverage.  That is
    correct/expected behavior — the test only asserts no crash and no silent drop.
    """
    m = _load()
    g = {
        "nodes": [
            {"id": "known", "topo_rank": 0, "blocks_objectives": []},
            {"id": "unknown-proj", "topo_rank": 0, "blocks_objectives": []},
        ],
        "edges": [],
    }
    # 'unknown-proj' has no entry in lineage — its lane is absent/None
    lin = {"known": {"advances": "career"}}
    # Must not raise; edge must appear in exactly one of the two lists.
    out = m.classify_edges(
        [{"from": "known", "to": "unknown-proj", "type": "requires"}], g, lin
    )
    total = len(out["critical"]) + len(out["auto_accepted"])
    assert total == 1, "edge must not be silently dropped"
    # The edge must not be classified as cross-lane (unknown lane → None → guard short-circuits).
    for entry in out["critical"]:
        assert entry.get("reason") != "cross-lane", (
            "unknown lane must not trigger cross-lane; classify_edges must not crash"
        )
