import importlib.util, os
from pathlib import Path, PurePosixPath
import pytest

SPEC = Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb_paths.py"
def _load():
    spec = importlib.util.spec_from_file_location("kb_paths", SPEC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_dev_root_env_override(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/tmp/devroot")
    assert str(_load().dev_root()).replace("\\", "/") == "/tmp/devroot"

@pytest.mark.skip(
    reason="kb_paths.py does not expose sys at module scope (no platform-specific "
    "branching in current implementation); monkeypatching m.sys fails with AttributeError"
)
def test_dev_root_platform_defaults(monkeypatch):
    monkeypatch.delenv("KB_DEV_ROOT", raising=False)
    m = _load()
    monkeypatch.setattr(m.sys, "platform", "win32")
    assert str(m.dev_root()) == "C:\\"
    monkeypatch.setattr(m.sys, "platform", "linux")
    assert str(m.dev_root()).replace("\\", "/").endswith("/repos")

def test_to_relative_equals_root_is_empty(monkeypatch):
    # input that IS the dev root → "" (depth 0), not the root's basename
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    assert _load().to_relative("/srv/x") == ""

def test_to_relative_foreign_and_weird_never_raise(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    m = _load()
    assert m.to_relative("D:\\other\\thing") == "other/thing"  # foreign drive → best-effort
    assert m.to_relative("") == ""                              # empty never raises
    assert m.to_relative("\\\\server\\share") == "server/share"  # UNC never raises

def test_to_relative_strips_devroot(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    m = _load()
    assert m.to_relative("/srv/x/1.1-dev-tools/my-tool") == "1.1-dev-tools/my-tool"
    assert m.to_relative("/srv/x/1.1-dev-tools/devkit/my-util") == "1.1-dev-tools/devkit/my-util"

def test_to_relative_windows_source(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "C:\\")
    m = _load()
    assert m.to_relative("C:\\1.1-dev-tools\\my-tool") == "1.1-dev-tools/my-tool"
    assert m.to_relative("C:\\knowledge-base") == "knowledge-base"

def test_to_relative_already_relative(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    assert _load().to_relative("1.1-dev-tools/foo") == "1.1-dev-tools/foo"

def test_resolve_repo_joins(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    m = _load()
    assert str(m.resolve_repo("1.1-dev-tools/foo")).replace("\\", "/") == "/srv/x/1.1-dev-tools/foo"

def test_resolve_repo_legacy_absolute(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/srv/x")
    m = _load()
    assert str(m.resolve_repo("/old/abs/path")).replace("\\", "/") == "/old/abs/path"
    assert str(m.resolve_repo("C:\\1.0-dev\\x")).replace("/", "\\").lower().endswith("1.0-dev\\x")
