#!/usr/bin/env python3
"""Shared staleness engine for the kb-sync knowledge base.

Public API:
    compute(vault_root) -> dict[str, dict]

Maps each project name (card filename stem) to a freshness record:
    {
        "state":          "fresh" | "stale" | "very_stale" | "unknown",
        "head":           str | None,           # current HEAD sha
        "documented_sha": str | None,           # last-documented-sha from card
        "drift_commits":  int | None,           # commits ahead of documented sha
        "drift_age_days": int | None,           # days since oldest undocumented commit
    }

State band definitions:
    fresh      – HEAD == documented sha (drift_commits == 0)
    stale      – HEAD > documented sha, drift_age_days <= STALE_DAYS
    very_stale – HEAD > documented sha, drift_age_days > STALE_DAYS
    unknown    – path missing / not a dir / not a git repo / sha not found / any error

Vault layout assumed: <vault_root>/02-projects/<group>/<name>.md
Cards containing `path:` and `last-documented-sha:` in YAML frontmatter.

CLI usage:
    py -3 kb-staleness.py --vault <vault>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunable constant
# ---------------------------------------------------------------------------

STALE_DAYS: int = 14
"""Drift older than this many days → very_stale; at-or-under → stale."""

# ---------------------------------------------------------------------------
# Optional: reuse frontmatter helpers from kb-graph.py (hyphenated; importlib).
# We load lazily and fall back to the built-in scanner if the load fails.
# ---------------------------------------------------------------------------

_KB_GRAPH: object | None = None
_KB_GRAPH_LOADED: bool = False

_KB_PATHS: object | None = None
_KB_PATHS_LOADED: bool = False


def _load_kb_paths() -> object | None:
    """Load kb_paths.py once; return module or None on failure."""
    global _KB_PATHS, _KB_PATHS_LOADED
    if _KB_PATHS_LOADED:
        return _KB_PATHS
    _KB_PATHS_LOADED = True
    try:
        skill_dir = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "kb_paths", skill_dir / "kb_paths.py"
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _KB_PATHS = mod
    except Exception:
        _KB_PATHS = None
    return _KB_PATHS


def _load_kb_graph() -> object | None:
    """Load kb-graph.py once; return module or None on failure."""
    global _KB_GRAPH, _KB_GRAPH_LOADED
    if _KB_GRAPH_LOADED:
        return _KB_GRAPH
    _KB_GRAPH_LOADED = True
    try:
        skill_dir = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "kb_graph", skill_dir / "kb-graph.py"
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _KB_GRAPH = mod
    except Exception:
        _KB_GRAPH = None
    return _KB_GRAPH


# ---------------------------------------------------------------------------
# Frontmatter extraction helpers
# ---------------------------------------------------------------------------

def _extract_fm_text(text: str) -> str:
    """Return the raw YAML frontmatter block (between the --- fences) or ''."""
    mod = _load_kb_graph()
    if mod is not None and hasattr(mod, "_extract_fm_text"):
        return mod._extract_fm_text(text)  # type: ignore[attr-defined]
    # Fallback: inline implementation (identical logic)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    fm_lines: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            return "\n".join(fm_lines)
        fm_lines.append(ln)
    return ""  # unterminated


def _parse_scalar(fm_text: str, key: str) -> str:
    """Return the scalar value for *key*, or '' if absent/empty."""
    mod = _load_kb_graph()
    if mod is not None and hasattr(mod, "_parse_scalar"):
        return mod._parse_scalar(fm_text, key)  # type: ignore[attr-defined]
    # Fallback: inline implementation
    pattern = re.compile(r"^" + re.escape(key) + r":\s*(.*)", re.MULTILINE)
    m = pattern.search(fm_text)
    if not m:
        return ""
    val = m.group(1).strip().strip('"').strip("'")
    return val


# ---------------------------------------------------------------------------
# Card discovery
# ---------------------------------------------------------------------------

def _discover_cards(vault_root: Path) -> list[Path]:
    """Return all project cards (*.md, excluding _INDEX.md) under 02-projects/."""
    projects_dir = vault_root / "02-projects"
    if not projects_dir.is_dir():
        return []
    cards: list[Path] = []
    for md in sorted(projects_dir.rglob("*.md")):
        if md.name.startswith("_"):
            continue
        cards.append(md)
    return cards


def _read_card_fields(card: Path) -> tuple[str | None, str | None]:
    """Read (path_value, last_documented_sha) from a card's frontmatter.

    Returns (None, None) if either field is absent or the card can't be read.
    Strips surrounding single/double quotes from path (Windows paths use single quotes).
    """
    try:
        text = card.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None

    fm_text = _extract_fm_text(text)
    if not fm_text:
        return None, None

    path_val = _parse_scalar(fm_text, "path")
    sha_val = _parse_scalar(fm_text, "last-documented-sha")

    if not path_val or not sha_val:
        return None, None

    # _parse_scalar already strips surrounding quotes; normalise Windows sep too.
    path_val = path_val.strip("'\"").strip()
    sha_val = sha_val.strip("'\"").strip()

    return path_val or None, sha_val or None


# ---------------------------------------------------------------------------
# Git subprocess helper
# ---------------------------------------------------------------------------

_CREATE_NO_WINDOW: int = 0x08000000  # win32 flag — suppresses console flash


def _run_git(repo_path: str, *args: str) -> tuple[int, str]:
    """Run `git -C <repo_path> <args>` and return (returncode, stdout).

    On any failure (timeout, OSError, etc.) returns (-1, "").
    Never raises.  UTF-8 decode, errors='replace' so weird commits don't crash.
    """
    cmd = ["git", "-C", repo_path, *args]
    kwargs: dict = dict(
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, **kwargs)
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return -1, ""
    except Exception:
        return -1, ""


# ---------------------------------------------------------------------------
# Staleness for a single project
# ---------------------------------------------------------------------------

_UNKNOWN: dict = {
    "state": "unknown",
    "head": None,
    "documented_sha": None,
    "drift_commits": None,
    "drift_age_days": None,
}


def _staleness_for(repo_path_str: str, documented_sha: str) -> dict:
    """Compute staleness for one project repo.

    Returns a dict with keys: state, head, documented_sha, drift_commits, drift_age_days.
    Never raises — returns unknown on any error.
    """
    base = dict(_UNKNOWN)
    base["documented_sha"] = documented_sha

    # Step 1: path must exist and be a directory.
    repo_path = Path(repo_path_str)
    if not repo_path.is_dir():
        return base

    # Step 2: get current HEAD.
    rc, head = _run_git(repo_path_str, "rev-parse", "HEAD")
    if rc != 0 or not head:
        return base
    base["head"] = head

    # Step 3: verify the documented sha exists in the repo and resolve it to a
    # full sha.  Capturing the resolved sha lets step 4 do an exact equality
    # check rather than a substring match, avoiding the empty-prefix trap.
    rc2, resolved = _run_git(
        repo_path_str, "rev-parse", "--verify", f"{documented_sha}^{{commit}}"
    )
    if rc2 != 0 or not resolved:
        # documented sha not found; head is known — return it so callers
        # (/kb-status) can still display the current HEAD alongside "unknown".
        return base

    # Step 4: count commits between documented sha and HEAD.
    rc3, count_str = _run_git(
        repo_path_str, "rev-list", "--count", f"{documented_sha}..HEAD"
    )
    if rc3 != 0:
        return base
    try:
        drift_commits = int(count_str)
    except ValueError:
        return base

    if drift_commits == 0:
        # Confirm HEAD truly equals the documented sha — a repo that was reset
        # --hard to a point *before* the documented sha also yields count=0 but
        # is diverged, not fresh.  In that case we return unknown rather than
        # silently mis-classifying the rollback.
        if head == resolved:
            return {
                "state": "fresh",
                "head": head,
                "documented_sha": documented_sha,
                "drift_commits": 0,
                "drift_age_days": 0,
            }
        # HEAD diverged from documented sha without advancing past it.
        return base

    # Step 5: oldest undocumented commit timestamp.
    rc4, oldest_log = _run_git(
        repo_path_str, "log", "--reverse", "--format=%ct", f"{documented_sha}..HEAD"
    )
    if rc4 != 0 or not oldest_log:
        return base

    lines = oldest_log.splitlines()
    if not lines:
        return base
    try:
        oldest_ts = int(lines[0].strip())
    except ValueError:
        return base

    # Step 6: compute age in days (UTC, timezone-aware).
    # Guard against pathological/corrupt commit timestamps that cause
    # datetime.fromtimestamp to raise OverflowError, OSError, or ValueError.
    now = datetime.now(timezone.utc)
    try:
        oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return base
    drift_age_days = (now - oldest_dt).days

    state = "stale" if drift_age_days <= STALE_DAYS else "very_stale"

    return {
        "state": state,
        "head": head,
        "documented_sha": documented_sha,
        "drift_commits": drift_commits,
        "drift_age_days": drift_age_days,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute(vault_root: str | Path) -> dict[str, dict]:
    """Return staleness records for all project cards in the vault.

    Parameters
    ----------
    vault_root:
        Path to the Obsidian vault root (e.g. ``/home/you/repos/knowledge-base``).

    Returns
    -------
    dict mapping project name (card stem) to:
        {state, head, documented_sha, drift_commits, drift_age_days}
    """
    vault_root = Path(vault_root)
    cards = _discover_cards(vault_root)

    result: dict[str, dict] = {}
    for card in cards:
        name = card.stem
        try:
            path_val, sha_val = _read_card_fields(card)
            if path_val is None or sha_val is None:
                result[name] = dict(_UNKNOWN)
                continue
            kp = _load_kb_paths()
            abs_repo = str(kp.resolve_repo(path_val)) if kp is not None else path_val
            result[name] = _staleness_for(abs_repo, sha_val)
        except Exception:
            # Outer fence: a single malformed card must never crash the engine
            # for all other cards.  _read_card_fields and _staleness_for are
            # themselves fail-soft, so reaching here indicates an unexpected
            # error; return unknown for this card and keep processing.
            result[name] = dict(_UNKNOWN)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute staleness for all KB project cards."
    )
    parser.add_argument(
        "--vault",
        required=True,
        metavar="PATH",
        help="Path to the vault root (e.g. /home/you/repos/knowledge-base)",
    )
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    records = compute(args.vault)
    print(json.dumps(records, indent=2, default=str))


if __name__ == "__main__":
    _main()
