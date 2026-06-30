import json
from tools.reconciler import ops
from reconciler_helpers import make_project

def test_rename_full_migration(vault):
    make_project(vault, "secrets-kms", "1.1-dev-tools", path="1.1-dev-tools/secrets-kms")
    make_project(vault, "svcmesh", "1.0-dev")
    (vault / "00-meta/project-edges.yaml").write_text(
        '# header keep me\nsvcmesh:\n  requires: ["secrets-kms", "projgamma"]\n', encoding="utf-8")
    (vault / "04-cli-errors/cmd-secrets-kms-init.md").write_text(
        '---\ntype: cli\ntags: [type/cli, "tool/secrets-kms"]\ntool: "secrets-kms"\nrelated: ["[[secrets-kms]]"]\nup: "[[secrets-kms]]"\n---\n', encoding="utf-8")
    op = {"op": "rename", "old": "secrets-kms", "new": "kms",
          "to": "1.1-dev-tools/kms", "repo": "git@github.com:example-org/kms.git", "date": "2026-06-22"}
    ops.apply_rename(vault, op, commit=True)

    assert not (vault / "02-projects/1.1-dev-tools/secrets-kms.md").exists()
    card = (vault / "02-projects/1.1-dev-tools/kms.md").read_text()
    assert 'title: "kms"' in card and "secrets-kms" in card  # alias retained
    assert "tool/kms" in card and "tool/secrets-kms" not in card
    assert (vault / "00-meta/scout-cache/kms.json").exists()
    assert not (vault / "00-meta/scout-cache/secrets-kms.json").exists()
    assert json.loads((vault / "00-meta/scout-cache/kms.json").read_text())["identity"]["name"] == "kms"
    # Old owner's atomics are DELETED; kb-atomize (not run here) regenerates them under kms.
    assert not (vault / "04-cli-errors/cmd-secrets-kms-init.md").exists()
    assert not (vault / "04-cli-errors/cmd-kms-init.md").exists()
    edges = (vault / "00-meta/project-edges.yaml").read_text()
    assert "kms" in edges and "secrets-kms" not in edges and "projgamma" in edges and "header keep me" in edges
    man = {e["name"]: e for e in json.loads((vault / "00-meta/kb-manifest.json").read_text())}
    assert "kms" in man and "secrets-kms" not in man

def test_rename_wikilinks_with_alias_and_heading(vault):
    make_project(vault, "secrets-kms", "1.1-dev-tools", path="1.1-dev-tools/secrets-kms")
    note = vault / "04-cli-errors/note.md"
    note.write_text("See [[secrets-kms]], [[secrets-kms|the vault]], and [[secrets-kms#setup]].\n", encoding="utf-8")
    ops.apply_rename(vault, {"op":"rename","old":"secrets-kms","new":"kms","to":"1.1-dev-tools/kms","date":"2026-06-22"}, commit=True)
    txt = note.read_text()
    assert "[[kms]]" in txt and "[[kms|the vault]]" in txt and "[[kms#setup]]" in txt
    assert "secrets-kms" not in txt

def test_rename_idempotent(vault):
    make_project(vault, "kms", "1.1-dev-tools", path="1.1-dev-tools/kms")
    op = {"op": "rename", "old": "secrets-kms", "new": "kms", "to": "1.1-dev-tools/kms", "date": "2026-06-22"}
    assert ops.apply_rename(vault, op, commit=True) == []

def test_rename_dry_run_writes_nothing(vault):
    make_project(vault, "secrets-kms", "1.1-dev-tools", path="1.1-dev-tools/secrets-kms")
    plan = ops.apply_rename(vault, {"op":"rename","old":"secrets-kms","new":"kms","to":"1.1-dev-tools/kms","date":"2026-06-22"}, commit=False)
    assert plan
    assert (vault / "02-projects/1.1-dev-tools/secrets-kms.md").exists()
    assert not (vault / "02-projects/1.1-dev-tools/kms.md").exists()

def test_rename_leaves_sibling_untouched(vault):
    make_project(vault, "example-toolkit", "3.0-work", path="3.0-work/example-toolkit")
    make_project(vault, "example-toolkit-app", "3.0-work", path="3.0-work/example-toolkit-app")
    (vault / "04-cli-errors/cmd-example-toolkit-run.md").write_text(
        '---\ntype: cli\ntags: [type/cli, "tool/example-toolkit"]\ntool: "example-toolkit"\nrelated: ["[[example-toolkit]]"]\n---\n', encoding="utf-8")
    (vault / "04-cli-errors/cmd-example-toolkit-app-deploy.md").write_text(
        '---\ntype: cli\ntags: [type/cli, "tool/example-toolkit-app"]\ntool: "example-toolkit-app"\nrelated: ["[[example-toolkit-app]]"]\n---\n', encoding="utf-8")
    note = vault / "04-cli-errors/n.md"
    note.write_text("[[example-toolkit]] vs [[example-toolkit-app]]\n", encoding="utf-8")
    ops.apply_rename(vault, {"op":"rename","old":"example-toolkit","new":"sectk","to":"3.0-work/sectk","date":"2026-06-22"}, commit=True)
    assert (vault / "02-projects/3.0-work/example-toolkit-app.md").exists()
    assert (vault / "04-cli-errors/cmd-example-toolkit-app-deploy.md").exists()
    sib = (vault / "04-cli-errors/cmd-example-toolkit-app-deploy.md").read_text()
    assert 'tool/example-toolkit-app' in sib and 'tool: "example-toolkit-app"' in sib
    assert "[[example-toolkit-app]]" in note.read_text()
    assert not (vault / "04-cli-errors/cmd-example-toolkit-run.md").exists()  # old owner's atomic deleted
    assert "[[sectk]]" in note.read_text()

def test_rename_duplicate_cards_all_migrated(vault):
    make_project(vault, "dupp", "1.0-dev", path="1.0-dev/dupp")
    make_project(vault, "dupp", "5.0-home", path="5.0-home/dupp")
    ops.apply_rename(vault, {"op":"rename","old":"dupp","new":"newp","to":"1.0-dev/newp","date":"2026-06-22"}, commit=True)
    assert list((vault / "02-projects").rglob("dupp.md")) == []
    assert (vault / "02-projects/1.0-dev/newp.md").exists()

def test_rename_updates_scout_cache_top_level_name(vault):
    # Real scout-caches carry a top-level `name` — the owner key kb-atomize reads
    # (owner = scout["name"]). Leaving it stale resurrects <old> atomics on reproject.
    make_project(vault, "secrets-kms", "1.1-dev-tools", path="1.1-dev-tools/secrets-kms")
    cache = vault / "00-meta/scout-cache/secrets-kms.json"
    data = json.loads(cache.read_text())
    data["name"] = "secrets-kms"
    cache.write_text(json.dumps(data), encoding="utf-8")
    ops.apply_rename(vault, {"op":"rename","old":"secrets-kms","new":"kms","to":"1.1-dev-tools/kms","date":"2026-06-22"}, commit=True)
    out = json.loads((vault / "00-meta/scout-cache/kms.json").read_text())
    assert out["name"] == "kms" and out["identity"]["name"] == "kms"
