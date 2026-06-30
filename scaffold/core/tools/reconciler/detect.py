"""`reconciler detect` -- offline drift discovery (spec 02 D5/D6/D10).

Compares the current live scan (identity.scan_live) against the last-reconciled
baseline (identity.load_baseline) by kb_id, and PROPOSES rename/relocate/retire/new
ledger ops (+ reports collisions/unstamped). It NEVER writes the ledger -- the
operator confirms a proposal, then records + applies it (STAGE -> PAUSE -> SUBMIT).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.reconciler import identity


@dataclass
class DetectReport:
    proposals: list[dict[str, Any]] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    unstamped: list[str] = field(default_factory=list)


def detect(vault: Path) -> DetectReport:
    scan = identity.scan_live(vault)
    baseline = identity.load_baseline(vault)
    rep = DetectReport(collisions=scan.collisions, unstamped=scan.unstamped)
    live = scan.live  # kb_id -> {name, group, path}
    # A collided id is evicted from `live`; it is NOT gone, just ambiguous. Never
    # propose a retire (or any op) for it — it is reported as a collision to resolve.
    collided = {c["kb_id"] for c in scan.collisions}

    for kid, b in baseline.items():
        if kid in collided:
            continue
        l = live.get(kid)
        if l is None:
            rep.proposals.append({"op": "retire", "owner": b["name"], "id": kid})
        elif l["name"] != b["name"]:
            # a simultaneous group move is carried by `to` (full new path)
            rep.proposals.append({"op": "rename", "old": b["name"], "new": l["name"],
                                  "to": l["path"], "id": kid})
        elif l["group"] != b.get("group"):
            rep.proposals.append({"op": "relocate", "name": b["name"], "to": l["path"],
                                  "group": l["group"], "id": kid})
    for kid, l in live.items():
        if kid not in baseline:
            rep.proposals.append({"op": "new", "name": l["name"], "group": l["group"], "id": kid})
    return rep
