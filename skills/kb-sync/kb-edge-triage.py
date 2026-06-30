#!/usr/bin/env python3
"""Edge-triage: classify proposed lineage edges as critical vs auto-accepted.

Dependency-free. Reuses kb-graph.py's SCC engine + prerequisite-type set via
importlib (kb-graph.py is hyphenated). Pure `classify_edges`; CLI for the skill.
"""
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path

_TOP_QUARTILE = 0.75  # nodes at/above this topo_rank percentile are critical-path


def _kb_graph():
    spec = importlib.util.spec_from_file_location("kb_graph", Path(__file__).with_name("kb-graph.py"))
    assert spec and spec.loader, "kb-graph.py not found"
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def _prereq_adjacency(graph: dict, prereq_types) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {n["id"]: [] for n in graph.get("nodes", [])}
    for e in graph.get("edges", []):
        if e.get("type") in prereq_types:
            adj.setdefault(e["from"], []).append(e["to"])
            adj.setdefault(e["to"], [])
    return adj


def _rank_threshold(graph: dict) -> int:
    ranks = sorted(n.get("topo_rank", 0) for n in graph.get("nodes", []))
    if not ranks:
        return 0
    return ranks[int(len(ranks) * _TOP_QUARTILE)]


def classify_edges(proposed: list[dict], graph: dict, lineage: dict) -> dict:
    """Classify proposed edges as critical or auto-accepted.

    Edge schema: both ``proposed`` edges and ``graph["edges"]`` must use
    ``from``/``to``/``type`` keys.  Callers NOT going through the CLI must
    normalise on-disk ``s``/``d``/``t`` edges themselves — the CLI's
    ``_normalize_graph_edges`` does this automatically.

    ``lineage`` must map project-id → dict with at least an ``"advances"`` key
    (e.g. ``{"a": {"advances": "career"}}``).

    Returns ``{"critical": [...], "auto_accepted": [...]}`` where each critical
    entry carries a ``"reason"`` field.  Reasons are checked in priority order —
    first match wins:

    1. ``"cycle"``          — edge creates a dependency cycle (Tarjan SCC)
    2. ``"conflict-dupe"``  — edge already exists in the graph
    3. ``"cross-lane"``     — src and dst advance different strategic lanes
    4. ``"high-leverage"``  — src or dst is on the critical path (top-quartile
                              topo_rank) or blocks at least one objective
    """
    kg = _kb_graph()
    prereq = kg._PREREQ_TYPES
    base_adj = _prereq_adjacency(graph, prereq)
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    existing = {(e["from"], e["to"]) for e in graph.get("edges", [])}
    thresh = _rank_threshold(graph)

    critical, auto = [], []
    for edge in proposed:
        src, dst, etype = edge["from"], edge["to"], edge.get("type", "requires")
        reason = None
        # 1. cycle — add edge, re-run Tarjan, look for a new SCC of size > 1.
        if etype in prereq:
            adj = {k: list(v) for k, v in base_adj.items()}
            adj.setdefault(src, []).append(dst); adj.setdefault(dst, [])
            if any(len(scc) > 1 and src in scc and dst in scc for scc in kg._tarjan_sccs(adj)):
                reason = "cycle"
        # 2. conflict / duplicate
        if reason is None and (src, dst) in existing:
            reason = "conflict-dupe"
        # 3. cross-lane
        if reason is None:
            ls, ld = lineage.get(src, {}).get("advances"), lineage.get(dst, {}).get("advances")
            if ls and ld and ls != ld:
                reason = "cross-lane"
        # 4. high-leverage (critical-path or blocks an objective)
        if reason is None:
            for nid in (src, dst):
                n = by_id.get(nid, {})
                if n.get("blocks_objectives") or n.get("topo_rank", 0) >= thresh:
                    reason = "high-leverage"; break
        (critical if reason else auto).append({**edge, **({"reason": reason} if reason else {})})
    return {"critical": critical, "auto_accepted": auto}


def _normalize_graph_edges(graph: dict) -> dict:
    """Normalize kb-graph.py's on-disk edge schema (s/d/t) to classify_edges contract (from/to/type).

    graph.json emits edges as {"s": src, "d": dst, "t": type, ...}.
    The classify_edges contract expects {"from": src, "to": dst, "type": type}.
    Nodes already match the contract (id/topo_rank/blocks_objectives).
    """
    normalized_edges = []
    for e in graph.get("edges", []):
        if "from" in e:
            # Already in contract form — pass through unchanged.
            normalized_edges.append(e)
        else:
            normalized_edges.append({
                "from": e["s"],
                "to": e["d"],
                "type": e["t"],
                **{k: v for k, v in e.items() if k not in ("s", "d", "t")},
            })
    return {**graph, "edges": normalized_edges}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Classify proposed lineage edges.")
    ap.add_argument("--graph", required=True); ap.add_argument("--proposed", required=True)
    ap.add_argument("--vault", required=True)
    a = ap.parse_args(argv)
    graph = _normalize_graph_edges(json.loads(Path(a.graph).read_text(encoding="utf-8")))
    proposed = json.loads(Path(a.proposed).read_text(encoding="utf-8"))
    lineage = _kb_graph()._read_sidecar(Path(a.vault))
    print(json.dumps(classify_edges(proposed, graph, lineage), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
