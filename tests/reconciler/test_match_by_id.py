"""T4 — ops resolve their target by kb_id (via the identity baseline), so a ledger
op that carries a correct `id` still targets the right project even when the typed
name is stale. Also confirms the ledger accepts an optional `id` field."""
from tools.reconciler import ops, identity, ledger
from reconciler_helpers import make_project

KID = "11111111-1111-4111-8111-111111111111"


def test_validate_op_accepts_optional_id():
    # id is an accepted optional field; name-only ops remain valid (append-only history).
    ledger.validate_op({"op": "retire", "owner": "x", "date": "2026-06-22", "id": KID})
    ledger.validate_op({"op": "retire", "owner": "x", "date": "2026-06-22"})


def test_retire_resolves_target_via_id(vault):
    # The project lives under 'kms'; the op typed the stale old name but carries the id.
    make_project(vault, "kms", "1.1-dev-tools")
    identity.write_baseline(vault, {KID: {"name": "kms", "group": "1.1-dev-tools"}})
    op = {"op": "retire", "owner": "secrets-kms", "id": KID, "date": "2026-06-22"}
    ops.apply_retire(vault, op, commit=True)
    assert not (vault / "02-projects/1.1-dev-tools/kms.md").exists()


def test_retire_without_id_uses_literal_owner(vault):
    make_project(vault, "ghost", "1.0-dev")
    ops.apply_retire(vault, {"op": "retire", "owner": "ghost", "date": "2026-06-22"}, commit=True)
    assert not (vault / "02-projects/1.0-dev/ghost.md").exists()


def test_rename_uses_explicit_old_and_is_idempotent_with_id(vault):
    # rename migrates the EXPLICIT old->new; carrying an id must NOT redirect `old`
    # through the baseline (which after apply maps id->new) — else a 2nd apply self-renames.
    make_project(vault, "kms", "1.1-dev-tools", path="1.1-dev-tools/kms")
    op = {"op": "rename", "old": "kms", "new": "kms2", "id": KID,
          "to": "1.1-dev-tools/kms2", "date": "2026-06-22"}
    ops.apply_rename(vault, op, commit=True)
    assert not (vault / "02-projects/1.1-dev-tools/kms.md").exists()
    assert (vault / "02-projects/1.1-dev-tools/kms2.md").exists()
    # post-apply: baseline now maps the id to the NEW name; re-applying must be a no-op.
    identity.write_baseline(vault, {KID: {"name": "kms2", "group": "1.1-dev-tools"}})
    assert ops.apply_rename(vault, op, commit=True) == []


def test_apply_all_union_tombstones_resolved_name(vault):
    # absorb op carries id resolving to live 'kms' but a stale typed `from`;
    # retired-projects.txt must record the RESOLVED slug, not the stale literal.
    make_project(vault, "kms", "1.1-dev-tools")
    make_project(vault, "host", "1.0-dev")
    identity.write_baseline(vault, {KID: {"name": "kms", "group": "1.1-dev-tools"}})
    op = {"op": "absorb", "from": "stale", "into": "host", "subpath": "web/",
          "id": KID, "date": "2026-06-22"}
    ops.apply_all(vault, [op], commit=True)
    retired = (vault / "00-meta/retired-projects.txt").read_text(encoding="utf-8")
    assert "kms" in retired and "stale" not in retired


def test_cmd_apply_dry_run_does_not_refresh_baseline(vault):
    import argparse
    from tools.reconciler import reconciler
    (vault / "reconcile/ledger.yaml").write_text("[]\n", encoding="utf-8")
    args = argparse.Namespace(ledger=str(vault / "reconcile/ledger.yaml"),
                              vault=str(vault), commit=False)
    reconciler.cmd_apply(args)
    assert not (vault / "reconcile/identity.yaml").exists()


def test_cmd_record_binds_id(vault):
    # --id must reach the ledger op — this is what makes match-by-id reachable via the CLI.
    import argparse
    from tools.reconciler import reconciler, ledger
    ledger_path = vault / "reconcile/ledger.yaml"
    ledger_path.write_text("# ledger\n", encoding="utf-8")
    args = argparse.Namespace(
        op_type="retire", date="2026-06-22", kb_id=KID, owner="ghost",
        from_=None, into=None, subpath=None, old=None, new=None, to=None,
        repo=None, name=None, group=None, ledger=str(ledger_path),
    )
    reconciler.cmd_record(args)
    recorded = ledger.load_ledger(ledger_path)[-1]
    assert recorded["op"] == "retire" and recorded["id"] == KID
