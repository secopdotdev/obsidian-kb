"""Stable per-project identity: the .kb-id file + uuid4 helpers.

The .kb-id file at a repo root carries the project's invariant identity
(spec 02 D1/D8). harvest reads it into scout-cache; the reconciler matches
lifecycle ops on it (D3). `name` (dir basename) is only a mutable slug.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from tools.reconciler.vault import atomic_write

KB_ID_FILE = ".kb-id"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LINE_RE = re.compile(r"^\s*kb_id\s*:\s*(\S+)\s*$", re.MULTILINE)


def new_id() -> str:
    """Mint a fresh uuid4 identity string."""
    return str(uuid.uuid4())


def is_valid(kb_id: str) -> bool:
    return bool(_UUID_RE.match(kb_id.strip().lower()))


def read_kb_id(repo: Path | str) -> str | None:
    """Return the kb_id from <repo>/.kb-id, or None if the file is absent.

    Raises ValueError (naming the repo) on a present-but-malformed file.
    """
    p = Path(repo) / KB_ID_FILE
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    m = _LINE_RE.search(text)
    if not m or not is_valid(m.group(1)):
        raise ValueError(f"malformed {KB_ID_FILE} in {repo}: {text!r}")
    return m.group(1).strip().lower()


def write_kb_id(repo: Path | str, kb_id: str) -> bool:
    """Write <repo>/.kb-id only if absent. Returns True if written, False if a
    valid id already exists (idempotent; never overwrites a prior value).

    The id is normalised to lowercase so write-then-read round-trips exactly
    (read_kb_id lowercases on return). Single-writer: the only-if-absent guard
    is a check-then-act, safe because `reconciler stamp` runs sequentially in one
    process — it is not concurrency-safe across processes.
    """
    if not is_valid(kb_id):
        raise ValueError(f"invalid kb_id: {kb_id!r}")
    kb_id = kb_id.strip().lower()
    repo = Path(repo)
    if read_kb_id(repo) is not None:
        return False
    atomic_write(repo / KB_ID_FILE, f"kb_id: {kb_id}\n")
    return True
