#!/usr/bin/env python3
"""kb-change-detect.py — Phase-1 change detection (zero LLM cost).

Reads 00-meta/kb-manifest.json for last_documented_sha per project, runs
git rev-parse HEAD in each repo, and outputs only the repos where the sha
changed as a JSON work-list ready to pass to workflow.js repos[].

Usage:
  python3 kb-change-detect.py --vault <vault> [--dev-root <path>] [--all] [--repo <name>]
  python3 kb-change-detect.py --vault ~/repos/kb

Flags:
  --vault      Path to KB vault (required).
  --dev-root   Parent of concept-group dirs. Defaults to KB_DEV_ROOT env, then ~/repos.
  --all        Skip sha comparison — return all repos (full rebuild).
  --repo NAME  Return only this repo (skip sha check too; for targeted reruns).
  --json       Pretty-print (default); pass --no-pretty for compact.

Output (stdout): JSON array of repo work-list entries, each:
  {name, group, path, path_rel, head_sha, last_documented_sha, changed_files_count}
Only repos where head_sha != last_documented_sha are included (unless --all/--repo).

Exit codes: 0 = success (even if zero repos changed), 1 = manifest missing, 2 = other error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("kb_paths", Path(__file__).parent / "kb_paths.py")
    _mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _dev_root_fn = _mod.dev_root
    _to_relative_fn = _mod.to_relative
except Exception:
    import os

    def _dev_root_fn() -> Path:  # type: ignore[misc]
        e = os.environ.get("KB_DEV_ROOT")
        return Path(e) if e else Path.home() / "repos"

    def _to_relative_fn(p: str) -> str:  # type: ignore[misc]
        return p


# Groups to scan (matches SKILL.md doctrine)
_CONCEPT_GROUPS = ["1.0-dev", "1.1-dev-tools", "2.0-career", "3.0-work", "5.0-home"]
# Dirs inside a group that are not first-party repos (skip)
_SKIP_NAMES = {"standalone", ".claude", "__pycache__", ".git", "node_modules"}


def _git_rev(path: Path) -> str | None:
    """Return HEAD sha for repo at path, or None if not a git repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _changed_files_count(path: Path, old_sha: str, new_sha: str) -> int:
    """Return number of changed files between two shas (best-effort; 0 on error)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "diff", "--name-only", old_sha, new_sha],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return len([l for l in r.stdout.splitlines() if l.strip()])
    except Exception:
        pass
    return 0


def detect(
    vault: Path,
    dev_root: Path,
    force_all: bool = False,
    only_repo: str | None = None,
) -> list[dict]:
    manifest_path = vault / "00-meta" / "kb-manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest: list[dict] = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name: dict[str, dict] = {e["name"]: e for e in manifest}

    work_list: list[dict] = []

    # Build candidate repo list from concept-group dirs
    for group in _CONCEPT_GROUPS:
        group_dir = dev_root / group
        if not group_dir.is_dir():
            continue
        for repo_dir in sorted(group_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            name = repo_dir.name
            if name in _SKIP_NAMES or name.startswith("."):
                continue

            # Targeted run
            if only_repo and name != only_repo:
                continue

            head_sha = _git_rev(repo_dir)
            if head_sha is None:
                continue  # not a git repo

            manifest_entry = by_name.get(name, {})
            last_sha: str | None = manifest_entry.get("last_documented_sha") or None

            # Skip if unchanged (unless --all or --repo)
            if not force_all and not only_repo and last_sha and last_sha == head_sha:
                continue

            changed_count = 0
            if last_sha and last_sha != head_sha:
                changed_count = _changed_files_count(repo_dir, last_sha, head_sha)

            work_list.append({
                "name": name,
                "group": group,
                "path": str(repo_dir),
                "path_rel": _to_relative_fn(str(repo_dir)),
                "head_sha": head_sha,
                "last_documented_sha": last_sha or "",
                "changed_files_count": changed_count,
            })

    return work_list


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="Path to KB vault root")
    ap.add_argument("--dev-root", help="Parent of concept-group dirs (overrides KB_DEV_ROOT)")
    ap.add_argument("--all", action="store_true", dest="force_all",
                    help="Return all repos, ignoring sha comparison")
    ap.add_argument("--repo", metavar="NAME", help="Return only this repo")
    ap.add_argument("--no-pretty", action="store_true", help="Compact JSON output")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    dev_root = Path(args.dev_root).expanduser().resolve() if args.dev_root else _dev_root_fn()

    work_list = detect(vault, dev_root, force_all=args.force_all, only_repo=args.repo)

    indent = None if args.no_pretty else 2
    print(json.dumps(work_list, indent=indent))

    # Emit summary to stderr so stdout stays pure JSON
    skipped = "n/a (--all)" if args.force_all else "see manifest"
    print(
        f"# change-detect: {len(work_list)} repo(s) queued"
        + (f" (targeted: {args.repo})" if args.repo else ""),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
