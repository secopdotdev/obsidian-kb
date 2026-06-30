import subprocess
from tools.reconciler import stamp, idgen
from reconciler_helpers import make_project


def _setup(vault, tmp_path, monkeypatch, dirty=()):
    root = tmp_path / "devroot"
    for name, group in [("alpha", "1.0-dev"), ("beta", "1.0-dev")]:
        (root / group / name).mkdir(parents=True)
        make_project(vault, name, group)
    monkeypatch.setattr(stamp, "resolve_repo", lambda rel: root / rel)
    monkeypatch.setattr(stamp, "_is_dirty", lambda repo: repo.name in dirty)
    calls = []

    def fake_git(repo, *args):
        calls.append((repo.name, args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(stamp, "_git", fake_git)
    return root, calls


def test_dry_run_writes_nothing(vault, tmp_path, monkeypatch):
    root, calls = _setup(vault, tmp_path, monkeypatch)
    res = stamp.stamp(vault, commit=False)
    assert all(r.status == "would-write" for r in res)
    assert not (root / "1.0-dev/alpha/.kb-id").exists()
    assert calls == []


def test_commit_clean_commits_never_pushes(vault, tmp_path, monkeypatch):
    root, calls = _setup(vault, tmp_path, monkeypatch)
    res = stamp.stamp(vault, commit=True)
    assert all(r.status == "committed" for r in res)
    assert (root / "1.0-dev/alpha/.kb-id").exists()
    assert any(a[1][0] == "commit" for a in calls)
    assert not any(a[1] and a[1][0] == "push" for a in calls)


def test_commit_skips_dirty_untouched(vault, tmp_path, monkeypatch):
    root, _ = _setup(vault, tmp_path, monkeypatch, dirty={"beta"})
    res = {r.name: r for r in stamp.stamp(vault, commit=True)}
    assert res["beta"].status == "skipped-dirty"
    assert not (root / "1.0-dev/beta/.kb-id").exists()
    assert res["alpha"].status == "committed"


def test_existing_id_is_noop(vault, tmp_path, monkeypatch):
    root, _ = _setup(vault, tmp_path, monkeypatch)
    kid = idgen.new_id()
    idgen.write_kb_id(root / "1.0-dev/alpha", kid)
    res = {r.name: r for r in stamp.stamp(vault, commit=True)}
    assert res["alpha"].status == "exists" and res["alpha"].kb_id == kid


def test_commit_failure_rolls_back_partial_file(vault, tmp_path, monkeypatch):
    root = tmp_path / "devroot"
    (root / "1.0-dev/alpha").mkdir(parents=True)
    make_project(vault, "alpha", "1.0-dev")
    monkeypatch.setattr(stamp, "resolve_repo", lambda rel: root / rel)
    monkeypatch.setattr(stamp, "_is_dirty", lambda repo: False)

    def failing_git(repo, *args):
        rc = 1 if args and args[0] == "commit" else 0
        return subprocess.CompletedProcess(args, rc, "", "pre-commit hook rejected" if rc else "")

    monkeypatch.setattr(stamp, "_git", failing_git)
    res = {r.name: r for r in stamp.stamp(vault, commit=True)}
    assert res["alpha"].status == "error"
    # rollback: a re-run must be able to retry, so the partial .kb-id must be gone
    assert not (root / "1.0-dev/alpha/.kb-id").exists()


def test_missing_repo_is_non_destructive(vault, tmp_path, monkeypatch):
    root = tmp_path / "devroot"  # note: dir NOT created
    make_project(vault, "ghost", "1.0-dev")
    monkeypatch.setattr(stamp, "resolve_repo", lambda rel: root / rel)
    res = {r.name: r for r in stamp.stamp(vault, commit=True)}
    assert res["ghost"].status == "missing-repo"


def test_only_filter_limits_scope(vault, tmp_path, monkeypatch):
    root, _ = _setup(vault, tmp_path, monkeypatch)
    res = {r.name: r for r in stamp.stamp(vault, commit=True, only={"alpha"})}
    assert "alpha" in res and "beta" not in res


def test_malformed_manifest_entry_surfaced(vault, tmp_path, monkeypatch):
    import json
    man = json.loads((vault / "00-meta/kb-manifest.json").read_text())
    man.append({"name": "halfentry"})  # missing group
    (vault / "00-meta/kb-manifest.json").write_text(json.dumps(man), encoding="utf-8")
    monkeypatch.setattr(stamp, "resolve_repo", lambda rel: tmp_path / rel)
    res = {r.name: r for r in stamp.stamp(vault, commit=False)}
    assert res["halfentry"].status == "error"
