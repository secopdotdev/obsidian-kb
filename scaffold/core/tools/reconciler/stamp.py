"""`reconciler stamp` — backfill stable .kb-id across all live repos (spec 02 D7).

Writes .kb-id only-if-absent. With --commit, commits on the repo's CURRENT branch
ONLY if the tree is clean; dirty repos are left untouched and reported; never pushes.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.reconciler import idgen
from tools.reconciler.paths import resolve_repo
from tools.reconciler.vault import load_manifest


@dataclass
class StampResult:
    name: str
    kb_id: str | None
    status: str  # would-write | exists | committed | skipped-dirty | missing-repo | error
    detail: str = ""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


def _is_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain").stdout.strip())


def enumerate_repos(vault: Path) -> list[tuple[str, Path | None]]:
    """(name, abs repo dir) for every manifest entry. A malformed entry (missing
    name or group) yields (name-or-'<unknown>', None) so the caller surfaces it
    rather than silently dropping it (fail-fast, CLAUDE.md §5)."""
    out: list[tuple[str, Path | None]] = []
    for e in load_manifest(vault):
        name, group = e.get("name"), e.get("group")
        if name and group:
            out.append((name, resolve_repo(f"{group}/{name}")))
        else:
            out.append((name or "<unknown>", None))
    return out


def stamp(vault: Path, *, commit: bool, only: set[str] | None = None) -> list[StampResult]:
    results: list[StampResult] = []
    for name, repo in enumerate_repos(vault):
        if only and name not in only:
            continue
        if repo is None:
            results.append(StampResult(name, None, "error", "manifest entry missing name or group"))
            continue
        if not repo.exists():
            results.append(StampResult(name, None, "missing-repo", str(repo)))
            continue
        try:
            existing = idgen.read_kb_id(repo)
        except ValueError as exc:
            results.append(StampResult(name, None, "error", str(exc)))
            continue
        if existing is not None:
            results.append(StampResult(name, existing, "exists"))
            continue
        # Dirty check runs in BOTH modes so dry-run faithfully previews --commit.
        if _is_dirty(repo):
            results.append(StampResult(name, None, "skipped-dirty", str(repo)))
            continue
        if not commit:
            results.append(StampResult(name, None, "would-write", "absent; would mint .kb-id"))
            continue
        kid = idgen.new_id()
        idgen.write_kb_id(repo, kid)
        kb_id_path = repo / idgen.KB_ID_FILE
        add = _git(repo, "add", idgen.KB_ID_FILE)
        if add.returncode != 0:
            kb_id_path.unlink(missing_ok=True)  # roll back so a re-run can retry
            results.append(StampResult(name, kid, "error", f"git add failed: {add.stderr.strip()}"))
            continue
        cp = _git(repo, "commit", "-m", f"chore(kb): stamp stable kb_id {kid[:8]}")
        if cp.returncode == 0:
            results.append(StampResult(name, kid, "committed"))
        else:
            kb_id_path.unlink(missing_ok=True)  # roll back: never strand an uncommitted .kb-id
            results.append(StampResult(name, kid, "error", cp.stderr.strip()))
    return results
