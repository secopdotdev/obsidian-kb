#!/usr/bin/env python3
"""Per-machine dev-root resolution for the KB. Single source of truth shared by
kb-staleness.py and kb-migrate-paths.py. Mirror of the JS hooks' KB_DEV_ROOT logic.

Card path: fields are dev-root-RELATIVE posix (e.g. '1.1-dev-tools/my-tool').
Each machine resolves them against its own dev root:
  KB_DEV_ROOT env (required); else ~/repos on all platforms.
  Set KB_DEV_ROOT to the directory that contains your concept-group folders.
"""
from __future__ import annotations
import os
from pathlib import Path


def dev_root() -> Path:
    env = os.environ.get("KB_DEV_ROOT")
    if env:
        return Path(env)
    return Path.home() / "repos"


def _norm(p) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def to_relative(p: str) -> str:
    """Absolute (Windows or POSIX) -> dev-root-relative forward-slash. Idempotent on
    already-relative input. Best-effort (strip drive/leading slash) if not under dev_root,
    so migration never crashes on an unexpected path."""
    raw = str(p).replace("\\", "/")
    root = _norm(dev_root())
    low = raw.rstrip("/").lower()
    if low == root:
        # input IS the dev root → depth 0, no relative component
        return ""
    prefix = root + "/"
    if low.startswith(prefix):
        return raw[len(prefix):].strip("/")
    if not raw.startswith("/") and not (len(raw) > 1 and raw[1] == ":"):
        return raw.strip("/")
    out = raw
    if len(out) > 1 and out[1] == ":":
        out = out[2:]
    return out.strip("/")


def resolve_repo(rel: str) -> Path:
    """dev-root-relative -> absolute Path. A legacy absolute input is returned as-is."""
    s = str(rel)
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return Path(s)
    return dev_root() / s
