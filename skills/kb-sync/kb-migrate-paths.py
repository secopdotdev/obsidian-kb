#!/usr/bin/env python3
"""One-time: rewrite card path: frontmatter from absolute to dev-root-relative.
Run ONLY while the /kb-sync sentinel is active (cards are Guard-5 protected). Atomic, idempotent.
Usage: python3 kb-migrate-paths.py --vault <vault> [--dry-run]"""
from __future__ import annotations
import argparse, importlib.util, os, re, sys
from pathlib import Path

_kp = importlib.util.spec_from_file_location("kb_paths", Path(__file__).parent / "kb_paths.py")
KP = importlib.util.module_from_spec(_kp); _kp.loader.exec_module(KP)

# Matches:  path: 'value'  /  path: "value"  /  path: value  (first FM block only)
PATH_RE = re.compile(r"^(path:\s*)(['\"]?)(.+?)\2\s*$", re.MULTILINE)


def _is_relative(v: str) -> bool:
    """Return True if v is already dev-root-relative (no drive letter, no leading slash)."""
    return not v.startswith("/") and not (len(v) > 1 and v[1] == ":")


def migrate(vault: Path, dry_run: bool) -> list[str]:
    """Rewrite absolute path: values to dev-root-relative in every 02-projects card.

    Returns list of "filename: old -> new" for each changed card.
    Atomic write (temp + os.replace); preserves original line endings.
    """
    vault = Path(vault)
    changed: list[str] = []
    for md in sorted((vault / "02-projects").rglob("*.md")):
        if md.name.startswith("_"):
            continue
        # Detect line endings from raw bytes before text read normalises them
        raw_bytes = md.read_bytes()
        nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
        # read_text normalises \r\n → \n so the regex reliably matches
        text = md.read_text(encoding="utf-8")
        # Match only the FIRST frontmatter block (non-greedy)
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        pm = PATH_RE.search(fm)
        if not pm:
            continue
        val = pm.group(3).strip()
        if _is_relative(val):
            continue
        # to_relative normalises backslashes → forward-slashes; collapse any
        # double-slashes that arise from literal \\ in double-quoted YAML scalars
        rel = re.sub(r"/{2,}", "/", KP.to_relative(val))
        # Rebuild the frontmatter with only the path: line changed
        new_fm = fm[: pm.start()] + f"path: {rel}" + fm[pm.end():]
        # Splice back into full text (offsets are within the normalised \n text)
        new_text = text[: fm_match.start(1)] + new_fm + text[fm_match.end(1):]
        changed.append(f"{md.name}: {val} -> {rel}")
        if not dry_run:
            tmp = md.with_suffix(".md.tmp")
            # Write with the file's original line endings to keep non-path bytes byte-identical
            tmp.write_text(new_text, encoding="utf-8", newline=nl)
            os.replace(tmp, md)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True, help="Path to the KB vault root")
    ap.add_argument("--dry-run", action="store_true", help="List changes without writing")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    results = migrate(Path(a.vault), a.dry_run)
    for line in results:
        print(line)
    if not results:
        print("No changes." if not a.dry_run else "Nothing to migrate.")


if __name__ == "__main__":
    main()
