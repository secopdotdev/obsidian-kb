#!/usr/bin/env python3
"""kb-status.py — offline, read-only KB status view.

Produces a comprehensive operator view of the knowledge base:
  1. Per-project freshness table (worst-first ordering).
  2. Roll-up summary (counts per state; how many need /kb-sync).
  3. Graph health from 00-meta/lineage-quality.json (fail-soft if missing).
  4. Mission Control board pointer.

Public API (injectable for tests):
    build_status(vault_root, staleness=None) -> dict

CLI:
    py -3 kb-status.py --vault <vault>
    py -3 kb-status.py --vault <vault> --json
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Mapping

# ---------------------------------------------------------------------------
# Configurable constant — change this when the board moves to LAN.
# ---------------------------------------------------------------------------
BOARD_URL: str = "http://localhost:4321"

# Sort priority: lower number = worse = appears first.
_STATE_PRIORITY: dict[str, int] = {
    "very_stale": 0,
    "stale": 1,
    "unknown": 2,
    "fresh": 3,
}


# ---------------------------------------------------------------------------
# Load kb-staleness.py via importlib (hyphenated filename).
# ---------------------------------------------------------------------------

_KB_STALENESS_COMPUTE = None
_KB_STALENESS_LOADED: bool = False


def _load_staleness_compute():
    """Return the compute() callable from kb-staleness.py, or None on failure."""
    global _KB_STALENESS_COMPUTE, _KB_STALENESS_LOADED
    if _KB_STALENESS_LOADED:
        return _KB_STALENESS_COMPUTE
    _KB_STALENESS_LOADED = True
    try:
        skill_dir = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "kb_staleness", skill_dir / "kb-staleness.py"
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _KB_STALENESS_COMPUTE = getattr(mod, "compute", None)
    except Exception as exc:
        print(f"kb-staleness load failed: {exc}", file=sys.stderr)
        _KB_STALENESS_COMPUTE = None
    return _KB_STALENESS_COMPUTE


# ---------------------------------------------------------------------------
# Graph health — reads 00-meta/lineage-quality.json
# ---------------------------------------------------------------------------

def _load_graph_health(vault_root: Path) -> dict | None:
    """Read lineage-quality.json and return a health dict.

    Returns
    -------
    dict
        Parsed health fields when the file exists and is valid.
    ``{"_error": "unreadable"}``
        When the file exists but fails to parse or decode (JSONDecodeError,
        UnicodeDecodeError, etc.).  A stderr breadcrumb is printed.
    None
        When the file does not exist.
    """
    lq_path = vault_root / "00-meta" / "lineage-quality.json"
    if not lq_path.is_file():
        return None
    try:
        data = json.loads(lq_path.read_text(encoding="utf-8"))
        summary = data.get("summary", {})
        return {
            "node_count": summary.get("node_count"),
            "edge_count": summary.get("edge_count"),
            "dangling_count": summary.get("dangling_count"),
            "low_confidence_edge_count": summary.get("low_confidence_edge_count"),
            "generated_at": data.get("generated_at"),
        }
    except Exception as exc:
        print(f"lineage-quality.json unreadable: {exc}", file=sys.stderr)
        return {"_error": "unreadable"}


# ---------------------------------------------------------------------------
# Core status builder — injectable for tests
# ---------------------------------------------------------------------------

def build_status(
    vault_root: str | Path,
    staleness: Mapping[str, dict] | None = None,
) -> dict:
    """Build and return the full status dict.

    Parameters
    ----------
    vault_root:
        Path to the Obsidian vault root (e.g. ``/home/you/repos/knowledge-base``).
    staleness:
        Optional pre-computed staleness map with shape
        ``{project_name: {state, head, documented_sha, drift_commits, drift_age_days}}``.
        When None, ``kb-staleness.compute(vault_root)`` is called.
        Inject a synthetic map in tests to keep them hermetic (no real git).

    Returns
    -------
    dict with keys:
        ``projects``     — list of per-project dicts, sorted worst-first
        ``summary``      — roll-up counts dict
        ``graph``        — graph health dict or None if file missing
        ``board_url``    — str
    """
    vault_root = Path(vault_root)

    # --- Resolve staleness records ---
    if staleness is not None:
        records: dict[str, dict] = dict(staleness)
    else:
        compute = _load_staleness_compute()
        if compute is None:
            records = {}
        else:
            try:
                records = compute(vault_root)
            except Exception as exc:
                print(f"kb-staleness.compute() failed: {exc}", file=sys.stderr)
                records = {}

    # --- Build sorted project list ---
    projects: list[dict] = []
    for name, rec in records.items():
        state = rec.get("state") or "unknown"
        projects.append(
            {
                "name": name,
                "state": state,
                "drift_commits": rec.get("drift_commits"),
                "drift_age_days": rec.get("drift_age_days"),
                "head": rec.get("head"),
                "documented_sha": rec.get("documented_sha"),
            }
        )

    # Worst-first: primary = state priority, secondary = name (deterministic)
    projects.sort(key=lambda p: (_STATE_PRIORITY.get(p["state"], 99), p["name"]))

    # --- Roll-up summary (derived from compute() records, not from JSON) ---
    state_counts: dict[str, int] = {
        "very_stale": 0,
        "stale": 0,
        "unknown": 0,
        "fresh": 0,
    }
    for p in projects:
        st = p["state"]
        if st in state_counts:
            state_counts[st] += 1
        # states not in the known set are tallied under unknown
        else:
            state_counts["unknown"] += 1

    needs_sync = state_counts["stale"] + state_counts["very_stale"]

    summary = {
        "total": len(projects),
        "fresh": state_counts["fresh"],
        "stale": state_counts["stale"],
        "very_stale": state_counts["very_stale"],
        "unknown": state_counts["unknown"],
        "needs_sync": needs_sync,
    }

    # --- Graph health (fail-soft) ---
    graph = _load_graph_health(vault_root)

    return {
        "projects": projects,
        "summary": summary,
        "graph": graph,
        "board_url": BOARD_URL,
    }


# ---------------------------------------------------------------------------
# Renderers (pure str-returning; do NOT call sys.stdout.reconfigure here)
# ---------------------------------------------------------------------------

def render_text(status: dict) -> str:
    """Render the status dict as a human-readable text table."""
    lines: list[str] = []

    # Header
    lines.append("=" * 72)
    lines.append("  KB STATUS")
    lines.append("=" * 72)

    # --- Per-project freshness table ---
    lines.append("")
    lines.append("PROJECT FRESHNESS  (worst-first)")
    lines.append(
        f"  {'PROJECT':<36} {'STATE':<12} {'COMMITS':>7}  {'AGE (days)':>10}"
    )
    lines.append("  " + "-" * 68)

    projects = status.get("projects", [])
    if not projects:
        lines.append("  (no project cards found)")
    else:
        for p in projects:
            name = p["name"]
            state = p["state"]
            drift_commits = p["drift_commits"]
            drift_age = p["drift_age_days"]

            # Format numeric fields — show "-" when None (unknown state)
            commits_str = str(drift_commits) if drift_commits is not None else "-"
            age_str = str(drift_age) if drift_age is not None else "-"

            # State indicator
            if state == "very_stale":
                indicator = "(!)"
            elif state == "stale":
                indicator = "(~)"
            elif state == "unknown":
                indicator = "(?)"
            else:
                indicator = "   "

            lines.append(
                f"  {indicator} {name:<33} {state:<12} {commits_str:>7}  {age_str:>10}"
            )

    # --- Roll-up summary ---
    summary = status.get("summary", {})
    lines.append("")
    lines.append("SUMMARY")
    lines.append(f"  Total projects : {summary.get('total', 0)}")
    lines.append(
        f"  Fresh          : {summary.get('fresh', 0)}"
    )
    lines.append(
        f"  Stale          : {summary.get('stale', 0)}"
    )
    lines.append(
        f"  Very stale     : {summary.get('very_stale', 0)}"
    )
    lines.append(
        f"  Unknown        : {summary.get('unknown', 0)}"
    )
    needs = summary.get("needs_sync", 0)
    if needs == 0:
        lines.append(f"  Needs /kb-sync : {needs}  (all up to date)")
    else:
        lines.append(f"  Needs /kb-sync : {needs}  -- run /kb-sync to update")

    # --- Graph health ---
    lines.append("")
    lines.append("GRAPH HEALTH")
    graph = status.get("graph")
    if graph is None:
        lines.append(
            "  lineage-quality.json not found; run /kb-sync"
        )
    elif graph.get("_error") == "unreadable":
        lines.append(
            "  lineage-quality.json unreadable (corrupt or encoding error); run /kb-sync"
        )
    else:
        lines.append(f"  Nodes          : {graph.get('node_count', '?')}")
        lines.append(f"  Edges          : {graph.get('edge_count', '?')}")
        lines.append(f"  Dangling edges : {graph.get('dangling_count', '?')}")
        lines.append(
            f"  Low-conf edges : {graph.get('low_confidence_edge_count', '?')}"
        )
        generated_at = graph.get("generated_at")
        if generated_at:
            lines.append(f"  Generated at   : {generated_at}")

    # --- Board pointer ---
    lines.append("")
    board_url = status.get("board_url", BOARD_URL)
    lines.append(f"MISSION CONTROL  {board_url}")
    lines.append("=" * 72)

    return "\n".join(lines)


def render_json(status: dict) -> str:
    """Render the status dict as compact JSON."""
    return json.dumps(status, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline, read-only status view for the knowledge base."
    )
    parser.add_argument(
        "--vault",
        required=True,
        metavar="PATH",
        help="Path to the vault root (e.g. /home/you/repos/knowledge-base)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the full status as a single JSON object.",
    )
    args = parser.parse_args()

    # Reconfigure stdout to UTF-8 for Windows cp1252 consoles.
    # Guard: under some environments (pytest capture, piped output) stdout may
    # not support reconfigure — skip safely.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):
        pass

    status = build_status(args.vault)

    if args.as_json:
        print(render_json(status))
    else:
        print(render_text(status))


if __name__ == "__main__":
    _main()
