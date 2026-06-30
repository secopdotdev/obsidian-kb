import pytest
from tools.reconciler import idgen


def test_new_id_is_valid_uuid4():
    for _ in range(5):
        assert idgen.is_valid(idgen.new_id())


def test_read_absent_returns_none(tmp_path):
    assert idgen.read_kb_id(tmp_path) is None


def test_write_then_read_roundtrip(tmp_path):
    kid = idgen.new_id()
    assert idgen.write_kb_id(tmp_path, kid) is True
    assert idgen.read_kb_id(tmp_path) == kid
    assert (tmp_path / ".kb-id").read_text(encoding="utf-8") == f"kb_id: {kid}\n"


def test_write_is_idempotent_only_if_absent(tmp_path):
    first = idgen.new_id()
    assert idgen.write_kb_id(tmp_path, first) is True
    assert idgen.write_kb_id(tmp_path, idgen.new_id()) is False
    assert idgen.read_kb_id(tmp_path) == first


def test_malformed_raises_valueerror_naming_repo(tmp_path):
    import re
    (tmp_path / ".kb-id").write_text("kb_id: not-a-uuid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        idgen.read_kb_id(tmp_path)
    # message must name the repo (acceptance criterion)
    with pytest.raises(ValueError, match=re.escape(str(tmp_path))):
        idgen.read_kb_id(tmp_path)


def test_empty_or_keyless_file_is_malformed(tmp_path):
    (tmp_path / ".kb-id").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        idgen.read_kb_id(tmp_path)


def test_write_rejects_invalid_id_argument(tmp_path):
    with pytest.raises(ValueError, match="invalid kb_id"):
        idgen.write_kb_id(tmp_path, "not-a-uuid")
    assert not (tmp_path / ".kb-id").exists()


def test_write_normalises_case_for_roundtrip(tmp_path):
    upper = idgen.new_id().upper()
    assert idgen.write_kb_id(tmp_path, upper) is True
    assert idgen.read_kb_id(tmp_path) == upper.lower()
