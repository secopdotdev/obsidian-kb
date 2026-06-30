import json
from pathlib import Path
from tools.reconciler import ops, vault
from reconciler_helpers import make_project


def test_retire_deletes_card_and_purges_manifest(vault):
    make_project(vault, "k3s-bootstrap", "1.1-dev-tools")
    op = {"op": "retire", "owner": "k3s-bootstrap", "date": "2026-06-22"}
    plan = ops.apply_retire(vault, op, commit=True)
    assert not (vault / "02-projects/1.1-dev-tools/k3s-bootstrap.md").exists()
    man = json.loads((vault / "00-meta/kb-manifest.json").read_text())
    assert all(e["name"] != "k3s-bootstrap" for e in man)
    assert plan  # non-empty on first apply


def test_retire_removes_inbound_edges(vault):
    make_project(vault, "k3s-bootstrap", "1.1-dev-tools")
    make_project(vault, "projbeta", "1.0-dev")
    (vault / "00-meta/project-edges.yaml").write_text(
        'projbeta:\n  requires: ["k3s-bootstrap", "projgamma"]\n', encoding="utf-8")
    ops.apply_retire(vault, {"op": "retire", "owner": "k3s-bootstrap", "date": "2026-06-22"}, commit=True)
    txt = (vault / "00-meta/project-edges.yaml").read_text()
    assert "k3s-bootstrap" not in txt and "projgamma" in txt


def test_retire_idempotent(vault):
    make_project(vault, "x", "1.0-dev")
    op = {"op": "retire", "owner": "x", "date": "2026-06-22"}
    ops.apply_retire(vault, op, commit=True)
    assert ops.apply_retire(vault, op, commit=True) == []  # converged


def test_retire_dry_run_writes_nothing(vault):
    make_project(vault, "y", "1.0-dev")
    op = {"op": "retire", "owner": "y", "date": "2026-06-22"}
    plan = ops.apply_retire(vault, op, commit=False)
    assert plan  # plan computed
    assert (vault / "02-projects/1.0-dev/y.md").exists()  # but nothing written
    man = json.loads((vault / "00-meta/kb-manifest.json").read_text())
    assert any(e["name"] == "y" for e in man)


def test_retire_deletes_scout_cache(vault):
    from reconciler_helpers import make_project
    make_project(vault, "z", "1.0-dev")
    assert (vault / "00-meta/scout-cache/z.json").exists()
    ops.apply_retire(vault, {"op": "retire", "owner": "z", "date": "2026-06-22"}, commit=True)
    assert not (vault / "00-meta/scout-cache/z.json").exists()


def test_retire_preserves_edges_header(vault):
    from reconciler_helpers import make_project
    make_project(vault, "k3s-bootstrap", "1.1-dev-tools")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# operator-authored edges — do not delete this header\nprojbeta:\n  requires: ["k3s-bootstrap", "projgamma"]\n', encoding="utf-8")
    ops.apply_retire(vault, {"op": "retire", "owner": "k3s-bootstrap", "date": "2026-06-22"}, commit=True)
    txt = (vault / "00-meta/project-edges.yaml").read_text()
    assert "operator-authored edges" in txt          # header preserved
    assert "k3s-bootstrap" not in txt and "projgamma" in txt


def test_retire_adds_owner_to_retired_projects(vault):
    from reconciler_helpers import make_project
    make_project(vault, "w", "1.0-dev")
    ops.apply_retire(vault, {"op": "retire", "owner": "w", "date": "2026-06-22"}, commit=True)
    txt = (vault / "00-meta/retired-projects.txt").read_text()
    assert "w" in txt.split()                     # owner present
    # idempotent on the retired-projects surface: re-apply -> empty plan
    assert ops.apply_retire(vault, {"op": "retire", "owner": "w", "date": "2026-06-22"}, commit=True) == []


def test_retire_dry_run_writes_nothing_all_surfaces(vault):
    from reconciler_helpers import make_project
    make_project(vault, "d", "1.0-dev")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# header\nfoo:\n  requires: ["d"]\n', encoding="utf-8")
    before_edges = (vault / "00-meta/project-edges.yaml").read_text()
    before_retired = (vault / "00-meta/retired-projects.txt").read_text()
    ops.apply_retire(vault, {"op": "retire", "owner": "d", "date": "2026-06-22"}, commit=False)
    assert (vault / "00-meta/scout-cache/d.json").exists()
    assert (vault / "02-projects/1.0-dev/d.md").exists()
    assert (vault / "00-meta/project-edges.yaml").read_text() == before_edges
    assert (vault / "00-meta/retired-projects.txt").read_text() == before_retired


def test_retire_deletes_all_duplicate_cards(vault):
    from reconciler_helpers import make_project
    make_project(vault, "dup", "1.0-dev")
    make_project(vault, "dup", "1.1-dev-tools")   # second card, same name, different group
    ops.apply_retire(vault, {"op": "retire", "owner": "dup", "date": "2026-06-22"}, commit=True)
    assert not (vault / "02-projects/1.0-dev/dup.md").exists()
    assert not (vault / "02-projects/1.1-dev-tools/dup.md").exists()

def test_edges_output_is_flow_style_for_kb_graph(vault):
    # kb-graph parses project-edges.yaml list fields inline (same line); block-style
    # lists read as EMPTY and drop edges. The reconciler must emit flow-style lists.
    make_project(vault, "gone", "1.0-dev")
    make_project(vault, "keep", "1.0-dev")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# h\nkeep:\n  requires: ["gone", "projgamma"]\n', encoding="utf-8")
    ops.apply_retire(vault, {"op": "retire", "owner": "gone", "date": "2026-06-22"}, commit=True)
    txt = (vault / "00-meta/project-edges.yaml").read_text()
    assert "requires: [projgamma]" in txt   # FLOW style (kb-graph-parseable)
    assert "\n  - projgamma" not in txt      # NOT block style
