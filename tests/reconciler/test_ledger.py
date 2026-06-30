import pytest
from tools.reconciler import ledger, vault

def test_load_empty(tmp_path):
    p = tmp_path / "ledger.yaml"; p.write_text("# header\n")
    assert ledger.load_ledger(p) == []

def test_validate_rejects_unknown_op():
    with pytest.raises(ValueError):
        ledger.validate_op({"op": "frobnicate", "date": "2026-06-22"})

def test_validate_requires_date():
    with pytest.raises(ValueError):
        ledger.validate_op({"op": "retire", "owner": "x"})

def test_append_dedupes(tmp_path):
    p = tmp_path / "ledger.yaml"; p.write_text("# header\n")
    op = {"op": "retire", "owner": "x", "date": "2026-06-22"}
    ledger.append_op(p, op); ledger.append_op(p, op)
    assert ledger.load_ledger(p) == [op]
    assert p.read_text().startswith("# header")

def test_detect_snapshot():
    by_remote = {"git@github.com:example-org/projbeta.git": "projbeta"}
    assert vault.detect_snapshot("projbeta-feat-x", "git@github.com:example-org/projbeta.git", by_remote) is True
    assert vault.detect_snapshot("kms", "git@github.com:example-org/kms.git", by_remote) is False
