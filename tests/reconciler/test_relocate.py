import json
from tools.reconciler import ops
from reconciler_helpers import make_project

def test_relocate_moves_card_across_group(vault):
    make_project(vault, "resume", "5.0-home", path="5.0-home/resume")
    op = {"op": "relocate", "name": "resume", "to": "2.0-career/resume", "group": "2.0-career", "date": "2026-06-22"}
    plan = ops.apply_relocate(vault, op, commit=True)
    assert not (vault / "02-projects/5.0-home/resume.md").exists()
    new = vault / "02-projects/2.0-career/resume.md"
    assert new.exists() and "path: 2.0-career/resume" in new.read_text()
    man = {e["name"]: e for e in json.loads((vault / "00-meta/kb-manifest.json").read_text())}
    assert man["resume"]["group"] == "2.0-career"
    assert man["resume"]["card_path"] == "02-projects/2.0-career/resume.md"

def test_relocate_path_only_keeps_group(vault):
    make_project(vault, "projgamma", "1.0-dev", path="1.0-dev/projgamma")
    op = {"op": "relocate", "name": "projgamma", "to": "1.1-dev-tools/projgamma", "group": "1.1-dev-tools", "date": "2026-06-22"}
    ops.apply_relocate(vault, op, commit=True)
    assert (vault / "02-projects/1.1-dev-tools/projgamma.md").exists()

def test_relocate_idempotent(vault):
    make_project(vault, "projgamma", "1.1-dev-tools", path="1.1-dev-tools/projgamma")
    op = {"op": "relocate", "name": "projgamma", "to": "1.1-dev-tools/projgamma", "group": "1.1-dev-tools", "date": "2026-06-22"}
    assert ops.apply_relocate(vault, op, commit=True) == []

def test_relocate_dry_run_writes_nothing(vault):
    make_project(vault, "projgamma", "1.0-dev", path="1.0-dev/projgamma")
    op = {"op": "relocate", "name": "projgamma", "to": "1.1-dev-tools/projgamma", "group": "1.1-dev-tools", "date": "2026-06-22"}
    plan = ops.apply_relocate(vault, op, commit=False)
    assert plan
    assert (vault / "02-projects/1.0-dev/projgamma.md").exists()  # not moved

def test_relocate_does_not_touch_body_group_tag(vault):
    from reconciler_helpers import make_project
    card = make_project(vault, "proj", "5.0-home", path="5.0-home/proj")
    body = card.read_text() + "\nSee #group/5.0-home in the body and group/5.0-home prose.\n"
    card.write_text(body, encoding="utf-8")
    ops.apply_relocate(vault, {"op":"relocate","name":"proj","to":"2.0-career/proj","group":"2.0-career","date":"2026-06-22"}, commit=True)
    moved = (vault / "02-projects/2.0-career/proj.md").read_text()
    assert "#group/5.0-home in the body" in moved          # body tag UNTOUCHED
    assert "group/5.0-home prose" in moved                  # body prose UNTOUCHED
    assert 'group: "2.0-career"' in moved or "group: 2.0-career" in moved  # frontmatter updated

def test_relocate_preserves_crlf(vault):
    from reconciler_helpers import make_project
    card = make_project(vault, "crlfproj", "5.0-home", path="5.0-home/crlfproj")
    card.write_bytes(card.read_text().replace("\n","\r\n").encode("utf-8"))
    ops.apply_relocate(vault, {"op":"relocate","name":"crlfproj","to":"2.0-career/crlfproj","group":"2.0-career","date":"2026-06-22"}, commit=True)
    raw = (vault / "02-projects/2.0-career/crlfproj.md").read_bytes()
    assert b"\r\n" in raw and b"\n\n" not in raw.replace(b"\r\n", b"")  # CRLF preserved, no lone-LF introduced

def test_relocate_cleans_duplicate_source_card(vault):
    from reconciler_helpers import make_project
    make_project(vault, "twin", "1.0-dev", path="1.0-dev/twin")
    make_project(vault, "twin", "5.0-home", path="5.0-home/twin")
    ops.apply_relocate(vault, {"op":"relocate","name":"twin","to":"1.1-dev-tools/twin","group":"1.1-dev-tools","date":"2026-06-22"}, commit=True)
    remaining = list((vault / "02-projects").rglob("twin.md"))
    assert remaining == [vault / "02-projects/1.1-dev-tools/twin.md"]  # exactly one, at dest
