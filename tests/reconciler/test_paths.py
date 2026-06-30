from pathlib import Path
from tools.reconciler import paths


def test_dev_root_honors_env(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/custom/root")
    assert paths.dev_root() == Path("/custom/root")


def test_resolve_repo_relative_joins_dev_root(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/dev")
    assert paths.resolve_repo("1.0-dev/alpha") == Path("/dev") / "1.0-dev/alpha"


def test_resolve_repo_posix_absolute_passthrough(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/dev")
    assert paths.resolve_repo("/home/user/repos/foo") == Path("/home/user/repos/foo")


def test_resolve_repo_windows_drive_passthrough(monkeypatch):
    monkeypatch.setenv("KB_DEV_ROOT", "/dev")
    # an absolute Windows path (drive letter) must pass through unchanged
    assert paths.resolve_repo("Q:/foo/bar") == Path("Q:/foo/bar")
