"""Dev-root resolution for the reconciler (vendored mirror of
kb-sync/kb_paths.py so the reconciler stays self-contained in the KB repo).
KB_DEV_ROOT env, else ~/repos on all platforms. Set KB_DEV_ROOT to the
directory that contains your concept-group folders."""
from __future__ import annotations

import os
from pathlib import Path


def dev_root() -> Path:
    env = os.environ.get("KB_DEV_ROOT")
    if env:
        return Path(env)
    return Path.home() / "repos"


def resolve_repo(rel: str) -> Path:
    """dev-root-relative -> absolute. A legacy absolute input is returned as-is."""
    s = str(rel)
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return Path(s)
    return dev_root() / s
