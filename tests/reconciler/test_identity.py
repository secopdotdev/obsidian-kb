from pathlib import Path
from tools.reconciler import identity

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _devroot(tmp_path, monkeypatch, repos):
    """repos: list of (group, dirname, kb_id_or_None). Builds dev_root tree + .kb-id files."""
    from tools.reconciler import idgen
    root = tmp_path / "dev"
    for group, name, kid in repos:
        d = root / group / name
        d.mkdir(parents=True)
        if kid:
            idgen.write_kb_id(d, kid)
    monkeypatch.setattr(identity, "dev_root", lambda: root)
    return root


def _manifest(vault, groups):
    import json
    man = [{"name": f"p{i}", "group": g} for i, g in enumerate(groups)]
    (vault / "00-meta/kb-manifest.json").write_text(json.dumps(man), encoding="utf-8")


def test_scan_live_reports_clean_entry(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "alpha", A)])
    _manifest(vault, ["1.0-dev"])
    scan = identity.scan_live(vault)
    assert scan.live[A] == {"name": "alpha", "group": "1.0-dev", "path": "1.0-dev/alpha"}
    assert scan.collisions == [] and scan.unstamped == []


def test_scan_live_collision(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "fork-a", A), ("1.0-dev", "fork-b", A)])
    _manifest(vault, ["1.0-dev"])
    scan = identity.scan_live(vault)
    assert A not in scan.live
    assert scan.collisions and scan.collisions[0]["kb_id"] == A


def test_scan_live_unstamped(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "naked", None)])
    _manifest(vault, ["1.0-dev"])
    scan = identity.scan_live(vault)
    assert scan.live == {} and any("naked" in u for u in scan.unstamped)


def test_baseline_roundtrip(vault):
    mapping = {A: {"name": "alpha", "group": "1.0-dev"}}
    identity.write_baseline(vault, mapping)
    assert (vault / "reconcile/identity.yaml").exists()
    assert identity.load_baseline(vault) == mapping


def test_load_baseline_absent_returns_empty(vault):
    assert identity.load_baseline(vault) == {}


def test_refresh_baseline_snapshots_live(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "alpha", A), ("3.0-work", "beta", B)])
    _manifest(vault, ["1.0-dev", "3.0-work"])
    out = identity.refresh_baseline(vault)
    assert out == {A: {"name": "alpha", "group": "1.0-dev"}, B: {"name": "beta", "group": "3.0-work"}}
    assert identity.load_baseline(vault) == out


def test_resolve_name_via_baseline(vault):
    identity.write_baseline(vault, {A: {"name": "kms", "group": "1.1-dev-tools"}})
    assert identity.resolve_name(vault, {"op": "retire", "owner": "secrets-kms", "id": A}, key="owner") == "kms"


def test_resolve_name_fallback_without_id(vault):
    assert identity.resolve_name(vault, {"op": "retire", "owner": "ghost"}, key="owner") == "ghost"


def test_load_baseline_tolerates_corrupt_file(vault):
    (vault / "reconcile/identity.yaml").write_text(":::not: valid: yaml: [", encoding="utf-8")
    assert identity.load_baseline(vault) == {}  # degrades, never crashes


def test_load_baseline_tolerates_non_dict_top_level(vault):
    (vault / "reconcile/identity.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert identity.load_baseline(vault) == {}


def test_refresh_baseline_preserves_on_empty_scan(vault, tmp_path, monkeypatch):
    identity.write_baseline(vault, {A: {"name": "alpha", "group": "1.0-dev"}})
    _devroot(tmp_path, monkeypatch, [])  # dev_root with no repos -> empty scan
    _manifest(vault, ["1.0-dev"])
    out = identity.refresh_baseline(vault)
    assert out == {A: {"name": "alpha", "group": "1.0-dev"}}  # prior preserved, not clobbered


def test_scan_live_skips_hidden_and_underscore_dirs(vault, tmp_path, monkeypatch):
    root = _devroot(tmp_path, monkeypatch, [("1.0-dev", "real", A)])
    (root / "1.0-dev/.git").mkdir()
    (root / "1.0-dev/_archive").mkdir()
    _manifest(vault, ["1.0-dev"])
    scan = identity.scan_live(vault)
    assert set(scan.live) == {A}
    assert not any(".git" in u or "_archive" in u for u in scan.unstamped)


def test_resolve_name_accepts_preloaded_baseline(vault):
    bl = {A: {"name": "kms", "group": "1.1-dev-tools"}}
    assert identity.resolve_name(vault, {"id": A}, key="name", baseline=bl) == "kms"


def test_cmd_stamp_dry_run_does_not_write_baseline(vault, monkeypatch):
    import argparse
    from tools.reconciler import reconciler
    _manifest(vault, ["1.0-dev"])
    args = argparse.Namespace(vault=str(vault), commit=False, only=None)
    reconciler.cmd_stamp(args)
    assert not (vault / "reconcile/identity.yaml").exists()
