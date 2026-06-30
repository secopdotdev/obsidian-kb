from tools.reconciler import detect, identity

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _devroot(tmp_path, monkeypatch, repos):
    root = tmp_path / "dev"
    for group, name, kid in repos:
        d = root / group / name
        d.mkdir(parents=True)
        if kid:
            identity.idgen.write_kb_id(d, kid)
    monkeypatch.setattr(identity, "dev_root", lambda: root)
    return root


def _baseline(vault, mapping):
    identity.write_baseline(vault, mapping)


def _manifest(vault, groups):
    import json
    man = [{"name": f"p{i}", "group": g} for i, g in enumerate(groups)]
    (vault / "00-meta/kb-manifest.json").write_text(json.dumps(man), encoding="utf-8")


def test_detect_rename(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("3.0-work", "example-toolkit", A)])
    _manifest(vault, ["3.0-work"])
    _baseline(vault, {A: {"name": "example-tookit", "group": "3.0-work"}})
    p = detect.detect(vault).proposals[0]
    assert p["op"] == "rename" and p["old"] == "example-tookit" and p["new"] == "example-toolkit"
    assert p["id"] == A and p["to"] == "3.0-work/example-toolkit"


def test_detect_relocate(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.1-dev-tools", "projgamma", A)])
    _manifest(vault, ["1.0-dev", "1.1-dev-tools"])
    _baseline(vault, {A: {"name": "projgamma", "group": "1.0-dev"}})
    p = detect.detect(vault).proposals[0]
    assert p["op"] == "relocate" and p["group"] == "1.1-dev-tools" and p["name"] == "projgamma"


def test_detect_retire(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [])
    _manifest(vault, ["1.0-dev"])
    _baseline(vault, {A: {"name": "k3s-bootstrap", "group": "1.0-dev"}})
    p = detect.detect(vault).proposals[0]
    assert p["op"] == "retire" and p["owner"] == "k3s-bootstrap" and p["id"] == A


def test_detect_new(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "shiny", B)])
    _manifest(vault, ["1.0-dev"])
    _baseline(vault, {A: {"name": "old", "group": "1.0-dev"}})  # A no longer live
    rep = detect.detect(vault)
    assert any(p["op"] == "new" and p["name"] == "shiny" and p["id"] == B for p in rep.proposals)


def test_detect_collision_reported_not_proposed(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "fork-a", A), ("1.0-dev", "fork-b", A)])
    _manifest(vault, ["1.0-dev"])
    _baseline(vault, {A: {"name": "fork-a", "group": "1.0-dev"}})
    rep = detect.detect(vault)
    assert rep.collisions and rep.collisions[0]["kb_id"] == A
    # a collided id is reported, NEVER proposed — not even a (spurious) retire,
    # even though it is in the baseline and absent from live.
    assert not any(p["id"] == A for p in rep.proposals)


def test_detect_clean_baseline_zero(vault, tmp_path, monkeypatch):
    _devroot(tmp_path, monkeypatch, [("1.0-dev", "alpha", A)])
    _manifest(vault, ["1.0-dev"])
    _baseline(vault, {A: {"name": "alpha", "group": "1.0-dev"}})
    rep = detect.detect(vault)
    assert rep.proposals == [] and rep.collisions == []
