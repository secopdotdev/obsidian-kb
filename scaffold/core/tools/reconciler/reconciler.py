"""
reconciler — durable KB repo-lifecycle reconciliation CLI.

Subcommands:
  status   — show ledger entries and convergence state (stub: converged=no)
  apply    — dry-run: compute desired state diff vs current vault (Task 6)
  record   — guided append of a new op to the ledger (Task 6)

Usage:
  py -3 tools/reconciler/reconciler.py status --ledger PATH [--vault PATH]
  py -3 tools/reconciler/reconciler.py apply --ledger PATH [--vault PATH] [--commit]
  py -3 tools/reconciler/reconciler.py record <op> [fields...] --ledger PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve repo root relative to this file's location so the CLI can be
# run from any working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Ensure the repo root is on sys.path so `from tools.reconciler import …`
# works when this script is invoked directly (e.g. py -3 tools/reconciler/reconciler.py).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_LEDGER = REPO_ROOT / "reconcile" / "ledger.yaml"
DEFAULT_VAULT = REPO_ROOT


def cmd_status(args: argparse.Namespace) -> int:
    """Print each ledger op and a convergence field.

    Convergence detection is a stub until the op handlers land in Task 6.
    Every op reports converged: no for now.
    """
    from tools.reconciler import ledger as ledger_mod

    ledger_path = Path(args.ledger)
    ops = ledger_mod.load_ledger(ledger_path)

    if not ops:
        print("ledger: (empty)")
        return 0

    for i, op in enumerate(ops, 1):
        fields = "  ".join(f"{k}={v}" for k, v in op.items())
        print(f"[{i}] {fields}  converged: no")

    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Dry-run (default) or commit ledger ops against the vault.

    Loads the ledger, calls apply_all, prints a table of mutations, and
    on --commit also prints the reproject follow-up commands.
    """
    import os
    from tools.reconciler import identity
    from tools.reconciler import ledger as ledger_mod
    from tools.reconciler import ops as ops_mod

    commit_flag: bool = getattr(args, "commit", False)

    ledger_path = Path(args.ledger)
    vault_path = Path(args.vault)

    ledger_ops = ledger_mod.load_ledger(ledger_path)
    plan = ops_mod.apply_all(vault_path, ledger_ops, commit=commit_flag)

    # Print the plan as a readable table.
    if plan:
        header = f"{'surface':<20} {'action':<20} {'path':<45} detail"
        print(header)
        print("-" * len(header))
        for m in plan:
            print(
                f"{m.get('surface',''):<20} "
                f"{m.get('action',''):<20} "
                f"{m.get('path',''):<45} "
                f"{m.get('detail','')}"
            )
        print()

    n = len(plan)
    if commit_flag:
        print(f"{n} mutations applied")
        # Print reproject follow-up commands.
        kb_skill_dir = os.environ.get("KB_SKILL_DIR") or (
            os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills", "kb-sync")
            if os.environ.get("CLAUDE_PLUGIN_ROOT")
            else "<kb-sync skill dir>"
        )
        kb_date = os.environ.get("KB_DATE")
        vault_str = str(vault_path)
        print()
        print("Next: run the reproject pipeline:")
        if not kb_date:
            print("  # set KB_DATE=YYYY-MM-DD (today) or substitute the date below before running")
        date_arg = kb_date if kb_date else "YYYY-MM-DD"
        print(
            f'  py -3 "{kb_skill_dir}/kb-atomize.py"'
            f" --cache 00-meta/scout-cache"
            f" --vault {vault_str}"
            f" --date {date_arg}"
        )
        print(
            f'  py -3 "{kb_skill_dir}/kb-index.py"'
            f" --vault {vault_str}"
            f" --out 00-meta/kb.sqlite"
        )
        # Refresh the identity baseline so a just-applied rename/relocate converges
        # (detect compares live vs this baseline — spec 02 D10).
        baseline = identity.refresh_baseline(vault_path)
        print(f"baseline refreshed: {len(baseline)} ids in reconcile/identity.yaml")
    else:
        print(f"{n} planned mutations")

    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Append a new op to the ledger via flags.

    Supports: retire --owner X
              absorb --from A --into B --subpath web/
              rename --old O --new N --to PATH [--repo R]
              relocate --name N --to PATH --group G
    """
    import os
    from tools.reconciler import ledger as ledger_mod

    ledger_path = Path(args.ledger)

    # Resolve date: --date arg takes priority, then KB_DATE env, else error.
    date_val: str = getattr(args, "date", None) or os.environ.get("KB_DATE", "")
    if not date_val:
        print(
            "error: a date is required — pass --date YYYY-MM-DD or set KB_DATE",
            file=sys.stderr,
        )
        return 1

    op_type: str = args.op_type

    if op_type == "retire":
        if not args.owner:
            print("error: retire requires --owner", file=sys.stderr)
            return 1
        op: dict[str, str] = {"op": "retire", "owner": args.owner, "date": date_val}

    elif op_type == "absorb":
        if not args.from_ or not args.into or not args.subpath:
            print("error: absorb requires --from, --into, --subpath", file=sys.stderr)
            return 1
        op = {
            "op": "absorb",
            "from": args.from_,
            "into": args.into,
            "subpath": args.subpath,
            "date": date_val,
        }

    elif op_type == "rename":
        if not args.old or not args.new:
            print("error: rename requires --old, --new", file=sys.stderr)
            return 1
        op = {"op": "rename", "old": args.old, "new": args.new, "date": date_val}
        if args.to:  # optional — matches apply_rename's op.get("to")
            op["to"] = args.to
        if args.repo:
            op["repo"] = args.repo

    elif op_type == "relocate":
        if not args.name or not args.to or not args.group:
            print("error: relocate requires --name, --to, --group", file=sys.stderr)
            return 1
        op = {
            "op": "relocate",
            "name": args.name,
            "to": args.to,
            "group": args.group,
            "date": date_val,
        }

    else:
        print(f"error: unknown op type {op_type!r}", file=sys.stderr)
        return 1

    # Bind the stable kb_id when provided (from `detect`). This is what makes the
    # match-by-id path (resolve_name / apply_all tombstoning) reachable via the CLI;
    # without it ops are name-only and a reused slug could target the wrong project.
    kb_id_val = getattr(args, "kb_id", None)
    if kb_id_val:
        op["id"] = kb_id_val

    ledger_mod.append_op(ledger_path, op)
    print(f"recorded: {op}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """Diff live scan vs baseline and PROPOSE lifecycle ops (never writes ledger).

    Classifies each kb_id:
      rename   -- same id, name changed
      relocate -- same id + name, group changed
      retire   -- id in baseline but absent from live
      new      -- id live but absent from baseline
    Collisions (same id in >=2 repos) are reported separately; they need manual resolution.
    """
    import json as json_mod
    from tools.reconciler import detect as detect_mod

    vault_path = Path(args.vault)
    rep = detect_mod.detect(vault_path)

    if getattr(args, "json", False):
        print(json_mod.dumps(
            {"proposals": rep.proposals, "collisions": rep.collisions, "unstamped": rep.unstamped},
            indent=2,
        ))
        return 0

    if not rep.proposals and not rep.collisions and not rep.unstamped:
        print("detect: baseline clean -- 0 proposals")
        return 0

    actionable = [p for p in rep.proposals if p["op"] != "new"]
    new_repos = [p for p in rep.proposals if p["op"] == "new"]
    if actionable:
        print(f"proposals ({len(actionable)}):")
        for p in actionable:
            label = p.get("old") or p.get("owner") or p.get("name") or ""
            target = p.get("new") or p.get("to") or ""
            arrow = f" -> {target}" if target else ""
            print(f"  {p['op']:9} {label}{arrow}  [{p['id'][:8]}]")

    if new_repos:
        # `new` is informational — there is no record/apply path; new repos enter via
        # kb-sync intake (then a future `stamp` covers them).
        print(f"\nnew/untracked ({len(new_repos)}) -- informational (add via kb-sync, not the ledger):")
        for p in new_repos:
            print(f"  {p['name']}  ({p['group']})  [{p['id'][:8]}]")

    if rep.collisions:
        print(f"\ncollisions ({len(rep.collisions)}) -- MANUAL RESOLUTION REQUIRED:")
        for c in rep.collisions:
            repos = ", ".join(c.get("repos", []))
            print(f"  {c['kb_id'][:8]}  repos: {repos}")

    if rep.unstamped:
        print(f"\nunstamped ({len(rep.unstamped)}) -- run: reconciler stamp --commit")
        for u in rep.unstamped:
            print(f"  {u}")

    if actionable:
        print("\nto apply an accepted op (carry the id so match-by-id works):")
        print("  reconciler record <op> ... --id <kb_id> && reconciler apply --commit")
    return 0


def cmd_stamp(args: argparse.Namespace) -> int:
    """Backfill .kb-id across all live repos.

    Dry-run by default; add --commit to write and commit the file on each repo's
    current branch (skips dirty trees, never pushes).
    """
    from tools.reconciler import identity
    from tools.reconciler import stamp as stamp_mod

    vault_path = Path(args.vault)
    only = set(args.only) if args.only else None
    results = stamp_mod.stamp(vault_path, commit=args.commit, only=only)

    if results:
        header = f"{'status':<14} {'name':<24} {'kb_id':<36} detail"
        print(header)
        print("-" * len(header))
        for r in results:
            print(f"{r.status:<14} {r.name:<24} {r.kb_id or '':<36} {r.detail}")
        print()

    from collections import Counter
    counts = Counter(r.status for r in results)
    summary = "  ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"total {len(results)}: {summary}")

    if args.commit:
        baseline = identity.refresh_baseline(vault_path)
        print(f"baseline refreshed: {len(baseline)} ids in reconcile/identity.yaml")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Durable KB repo-lifecycle reconciliation tool.",
    )

    # Shared options parent — added to each subparser so options can follow
    # the subcommand name (e.g. `reconciler.py status --ledger PATH`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER),
        help="Path to reconcile/ledger.yaml (default: repo root)",
    )
    common.add_argument(
        "--vault",
        default=str(DEFAULT_VAULT),
        help="Path to KB vault root (default: repo root)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # status
    sub.add_parser(
        "status",
        parents=[common],
        help="Show ledger entries and convergence state",
    )

    # apply
    apply_p = sub.add_parser(
        "apply",
        parents=[common],
        help="Compute and optionally apply vault mutations from the ledger",
    )
    apply_p.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Write mutations to disk (default: dry-run preview only)",
    )

    # stamp
    stamp_p = sub.add_parser(
        "stamp",
        parents=[common],
        help="Backfill .kb-id across all live repos (dry-run by default)",
    )
    stamp_p.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Write .kb-id and commit on each repo's current branch (default: dry-run)",
    )
    stamp_p.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="NAME",
        help="Limit to these project names only (one or more)",
    )

    # detect
    detect_p = sub.add_parser(
        "detect",
        parents=[common],
        help="Diff live scan vs baseline and propose lifecycle ops (offline, never writes ledger)",
    )
    detect_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON instead of a human summary",
    )

    # record
    record_p = sub.add_parser(
        "record",
        parents=[common],
        help="Append a new op to the ledger",
    )
    record_p.add_argument("op_type", metavar="op", help="Op type: retire|relocate|rename|absorb")
    record_p.add_argument("--date", default=None, help="Op date (YYYY-MM-DD); fallback: KB_DATE env")
    record_p.add_argument("--id", dest="kb_id", default=None,
                          help="Optional stable kb_id to bind the op to (copy from `detect` output)")
    # retire
    record_p.add_argument("--owner", default=None, help="[retire] Project owner name")
    # absorb
    record_p.add_argument("--from", dest="from_", default=None, help="[absorb] Source project name")
    record_p.add_argument("--into", default=None, help="[absorb] Host project name")
    record_p.add_argument("--subpath", default=None, help="[absorb] Subpath within host")
    # rename
    record_p.add_argument("--old", default=None, help="[rename] Old project name")
    record_p.add_argument("--new", default=None, help="[rename] New project name")
    record_p.add_argument("--to", default=None, help="[rename/relocate] New relative path")
    record_p.add_argument("--repo", default=None, help="[rename] New repo URL")
    # relocate
    record_p.add_argument("--name", default=None, help="[relocate] Project name")
    record_p.add_argument("--group", default=None, help="[relocate] New group name")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows console stdout is cp1252; plan details contain non-ASCII (e.g. '→').
    # Reconfigure to UTF-8 so printing never crashes (matches kb-sync scripts).
    for _stream in (sys.stdout, sys.stderr):
        _reconf = getattr(_stream, "reconfigure", None)
        if _reconf is not None:
            _reconf(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "status": cmd_status,
        "apply": cmd_apply,
        "record": cmd_record,
        "stamp": cmd_stamp,
        "detect": cmd_detect,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
