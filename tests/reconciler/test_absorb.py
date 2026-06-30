import json
from tools.reconciler import ops
from reconciler_helpers import make_project

def test_absorb_retires_from_and_records_provenance(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# header\nsiem-architecture:\n  requires: ["mission-control"]\n', encoding="utf-8")
    op = {"op": "absorb", "from": "mission-control", "into": "knowledge-base", "subpath": "web/", "date": "2026-06-22"}
    ops.apply_absorb(vault, op, commit=True)
    assert not (vault / "02-projects/1.1-dev-tools/mission-control.md").exists()
    assert not (vault / "00-meta/scout-cache/mission-control.json").exists()
    man = {e["name"]: e for e in json.loads((vault / "00-meta/kb-manifest.json").read_text())}
    assert "mission-control" not in man
    host = (vault / "02-projects/1.1-dev-tools/knowledge-base.md").read_text()
    assert "absorbed:" in host and "mission-control -> web/" in host
    edges = (vault / "00-meta/project-edges.yaml").read_text()
    assert "knowledge-base" in edges and "mission-control" not in edges and "header" in edges  # repointed, header kept

def test_absorb_does_not_delete_sibling_atomics(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    (vault / "04-cli-errors/cmd-mission-control-run.md").write_text(
        '---\ntype: cli\ntool: "mission-control"\n---\n', encoding="utf-8")
    (vault / "04-cli-errors/cmd-mission-control-extra-go.md").write_text(
        '---\ntype: cli\ntool: "mission-control-extra"\n---\n', encoding="utf-8")  # sibling owner
    ops.apply_absorb(vault, {"op":"absorb","from":"mission-control","into":"knowledge-base","subpath":"web/","date":"2026-06-22"}, commit=True)
    assert not (vault / "04-cli-errors/cmd-mission-control-run.md").exists()       # from's atomic deleted
    assert (vault / "04-cli-errors/cmd-mission-control-extra-go.md").exists()      # sibling untouched

def test_absorb_idempotent(vault):
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    op = {"op": "absorb", "from": "mission-control", "into": "knowledge-base", "subpath": "web/", "date": "2026-06-22"}
    # first apply: nothing to retire (no mission-control) but provenance gets added
    ops.apply_absorb(vault, op, commit=True)
    # second apply: fully converged
    assert ops.apply_absorb(vault, op, commit=True) == []

def test_absorb_dry_run_writes_nothing(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    plan = ops.apply_absorb(vault, {"op":"absorb","from":"mission-control","into":"knowledge-base","subpath":"web/","date":"2026-06-22"}, commit=False)
    assert plan
    assert (vault / "02-projects/1.1-dev-tools/mission-control.md").exists()

import yaml as _yaml

def test_absorb_merges_outbound_edges_src_before_dst(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# h\nmission-control:\n  requires: ["astro", "cytoscape"]\n'
        'knowledge-base:\n  requires: ["sqlite"]\n'
        'siem:\n  requires: ["mission-control", "knowledge-base"]\n', encoding="utf-8")
    ops.apply_absorb(vault, {"op":"absorb","from":"mission-control","into":"knowledge-base","subpath":"web/","date":"2026-06-22"}, commit=True)
    data = _yaml.safe_load((vault / "00-meta/project-edges.yaml").read_text())
    assert "mission-control" not in data
    assert set(data["knowledge-base"]["requires"]) == {"sqlite", "astro", "cytoscape"}  # merged
    assert data["siem"]["requires"] == ["knowledge-base"]  # repointed + deduped

def test_absorb_merges_outbound_edges_dst_before_src(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# h\nknowledge-base:\n  requires: ["sqlite"]\n'
        'mission-control:\n  requires: ["astro", "cytoscape"]\n', encoding="utf-8")
    ops.apply_absorb(vault, {"op":"absorb","from":"mission-control","into":"knowledge-base","subpath":"web/","date":"2026-06-22"}, commit=True)
    data = _yaml.safe_load((vault / "00-meta/project-edges.yaml").read_text())
    assert "mission-control" not in data
    assert set(data["knowledge-base"]["requires"]) == {"sqlite", "astro", "cytoscape"}  # merged, order-independent
