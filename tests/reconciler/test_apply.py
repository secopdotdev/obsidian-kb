import hashlib
from pathlib import Path
from tools.reconciler import ops, ledger, reconciler
from reconciler_helpers import make_project

def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()

def test_apply_all_dry_run_writes_nothing(vault):
    make_project(vault, "old", "1.0-dev")
    ops_list = [{"op": "retire", "owner": "old", "date": "2026-06-22"}]
    before = _tree_hash(vault)
    plan = ops.apply_all(vault, ops_list, commit=False)
    assert plan
    assert _tree_hash(vault) == before  # nothing written

def test_apply_all_commit_idempotent(vault):
    make_project(vault, "old", "1.0-dev")
    ops_list = [{"op": "retire", "owner": "old", "date": "2026-06-22"}]
    ops.apply_all(vault, ops_list, commit=True)
    assert ops.apply_all(vault, ops_list, commit=True) == []  # converged

def test_apply_all_retired_union_includes_absorb_from(vault):
    make_project(vault, "mission-control", "1.1-dev-tools")
    make_project(vault, "knowledge-base", "1.1-dev-tools", path="knowledge-base")
    make_project(vault, "gone", "1.0-dev")
    ops_list = [
        {"op": "retire", "owner": "gone", "date": "2026-06-22"},
        {"op": "absorb", "from": "mission-control", "into": "knowledge-base", "subpath": "web/", "date": "2026-06-22"},
    ]
    ops.apply_all(vault, ops_list, commit=True)
    owners = (vault / "00-meta/retired-projects.txt").read_text().split()
    assert "gone" in owners and "mission-control" in owners  # union of retire + absorb-from

def test_record_appends_op(tmp_path):
    p = tmp_path / "ledger.yaml"; p.write_text("# header\n")
    ledger.append_op(p, {"op": "retire", "owner": "x", "date": "2026-06-22"})
    assert ledger.load_ledger(p) == [{"op": "retire", "owner": "x", "date": "2026-06-22"}]

def test_validate_op_rejects_missing_required_field():
    import pytest
    with pytest.raises(ValueError):
        ledger.validate_op({"op": "retire", "date": "2026-06-22"})  # no owner
    with pytest.raises(ValueError):
        ledger.validate_op({"op": "absorb", "into": "x", "subpath": "web/", "date": "2026-06-22"})  # no from

def test_cli_record_then_apply_dry_run(vault, tmp_path):
    led = tmp_path / "ledger.yaml"; led.write_text("# header\n")
    make_project(vault, "old", "1.0-dev")
    rc = reconciler.main(["record", "retire", "--owner", "old",
                          "--ledger", str(led), "--vault", str(vault), "--date", "2026-06-22"])
    assert rc == 0
    assert ledger.load_ledger(led) == [{"op": "retire", "owner": "old", "date": "2026-06-22"}]
    # dry-run apply reports a non-zero planned count and writes nothing
    before = _tree_hash(vault)
    rc = reconciler.main(["apply", "--ledger", str(led), "--vault", str(vault)])
    assert rc == 0
    assert _tree_hash(vault) == before

def test_cli_apply_empty_ledger_zero_planned(vault, tmp_path, capsys):
    led = tmp_path / "ledger.yaml"; led.write_text("# header\n")
    reconciler.main(["apply", "--ledger", str(led), "--vault", str(vault)])
    assert "0 planned" in capsys.readouterr().out
