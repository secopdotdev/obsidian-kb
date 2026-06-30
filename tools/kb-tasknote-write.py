#!/usr/bin/env python3
"""kb-tasknote-write.py — TaskNote generator for the kb-sync pipeline.

For each active blocker on a project, creates or updates a TaskNote file in
``{vault}/TaskNotes/Tasks/``.  Resolved blockers (slug no longer present) are
marked ``status: done``.  When ``rag_flag == "green"`` the project's TaskNotes
are moved to ``{vault}/TaskNotes/Archive/``.

Public API
----------
    write_task_notes(project_slug, card_data, vault_root, *, dry_run=False) -> dict

CLI
---
    python kb-tasknote-write.py <project_slug> <project_card_json_path> <vault_root>
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Severity → tasknote priority mapping
# ---------------------------------------------------------------------------
SEVERITY_TO_PRIORITY: dict[str, str] = {
    "crit": "urgent",
    "high": "high",
    "med":  "medium",
    "low":  "low",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_task_notes(
    project_slug: str,
    card_data: dict[str, Any],
    vault_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Create, update, complete, or archive TaskNote files for a project's blockers.

    Args:
        project_slug:  Repo name / project identifier (e.g. ``"projbeta"``).
        card_data:     Merged dict from kb-sync pipeline; expected keys:
                         - ``"blockers"``: list[dict] with slug/text/severity/since/unblock
                         - ``"rag_flag"``: str (``"green"`` | ``"yellow"`` | ``"red"``)
                         - ``"group"``:    str vault group sub-folder (e.g. ``"1.0-dev"``)
        vault_root:    Absolute Path to the KB vault root.
        dry_run:       If True compute and log changes without writing anything.

    Returns:
        dict with integer counts for keys: ``created``, ``updated``,
        ``completed``, ``archived``.
    """
    counts: dict[str, int] = {
        "created": 0,
        "updated": 0,
        "completed": 0,
        "archived": 0,
    }

    rag_flag: str = card_data.get("rag_flag") or "red"
    raw_blockers: list[Any] = card_data.get("blockers") or []
    group: str = card_data.get("group") or ""
    now_utc: str = datetime.now(timezone.utc).isoformat(timespec="seconds")

    tasks_dir:   Path = vault_root / "TaskNotes" / "Tasks"
    archive_dir: Path = vault_root / "TaskNotes" / "Archive"

    # Normalise blockers — accept both dict and string forms
    blockers: list[dict[str, Any]] = []
    for item in raw_blockers:
        if isinstance(item, dict):
            blockers.append(item)
        elif isinstance(item, str) and item.strip():
            # Bare-string blocker: synthesise minimal slug from text
            slug = re.sub(r"[^a-z0-9]+", "-", item.lower().strip())[:60].strip("-")
            blockers.append({"slug": slug, "text": item, "severity": "low",
                             "since": "unknown", "unblock": ""})

    # O(1) active-slug lookup
    active_slugs: set[str] = {
        b["slug"] for b in blockers
        if isinstance(b, dict) and b.get("slug")
    }

    # ------------------------------------------------------------------
    # Phase 1 — upsert TaskNote for every active blocker
    # ------------------------------------------------------------------
    for blocker in blockers:
        slug:     str = blocker.get("slug", "").strip()
        text:     str = blocker.get("text", "").strip()
        severity: str = blocker.get("severity", "low")
        since:    str = blocker.get("since") or "unknown"
        unblock:  str = blocker.get("unblock") or "No unblock steps recorded."

        if not slug:
            continue  # malformed blocker — skip

        note_path = tasks_dir / f"{project_slug}--{slug}.md"

        # Never re-open a manually completed note
        existing_status = _read_tasknote_status(note_path)
        if existing_status == "done":
            continue

        priority = SEVERITY_TO_PRIORITY.get(severity, "low")
        # Preserve original creation timestamp and operator-set fields across re-runs
        is_new = not note_path.exists()
        created_ts = _read_tasknote_field(note_path, "created") or now_utc
        title_short = text[:80] + ("..." if len(text) > 80 else "")

        # Preserve operator-set status (e.g. "in-progress") and due date
        existing_op_status = existing_status if (existing_status and existing_status != "open") else "open"
        preserved_due = _read_tasknote_field(note_path, "due") or ""
        preserved_notes = _read_operator_notes(note_path)

        content = _render_tasknote(
            project_slug=project_slug,
            blocker_slug=slug,
            blocker_text_short=title_short,
            severity=severity,
            priority=priority,
            since=since,
            unblock=unblock,
            group=group,
            created=created_ts,
            modified=now_utc,
            status=existing_op_status,
            due=preserved_due,
            operator_notes=preserved_notes,
        )

        # True idempotency: skip atomic write when only `updated:` timestamp differs
        if not is_new and _content_unchanged(note_path, content):
            continue

        if not dry_run:
            _atomic_write(note_path, content)
        counts["created" if is_new else "updated"] += 1

    # ------------------------------------------------------------------
    # Phase 2a — rag_flag == "green": archive ALL project TaskNotes
    # ------------------------------------------------------------------
    if rag_flag == "green":
        pattern = f"{project_slug}--*.md"
        for note_path in tasks_dir.glob(pattern):
            dest = archive_dir / note_path.name
            if not dry_run:
                archive_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(str(note_path), str(dest))
                except OSError:
                    # Cross-device fallback: copy then delete
                    dest.write_text(
                        note_path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    note_path.unlink()
            counts["archived"] += 1

    # ------------------------------------------------------------------
    # Phase 2b — rag_flag != "green": mark resolved blockers as done
    # ------------------------------------------------------------------
    else:
        pattern = f"{project_slug}--*.md"
        for note_path in tasks_dir.glob(pattern):
            stem = note_path.stem  # "projbeta--projgamma-appliance-powered-off"
            prefix = f"{project_slug}--"
            if not stem.startswith(prefix):
                continue
            blocker_slug = stem[len(prefix):]

            if blocker_slug in active_slugs:
                continue  # still active — handled in Phase 1

            existing_status = _read_tasknote_status(note_path)
            if existing_status == "done":
                continue  # already completed — idempotent

            content = _patch_tasknote_done(note_path, now_utc)
            if content and not dry_run:
                _atomic_write(note_path, content)
            counts["completed"] += 1

    return counts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_tasknote_status(path: Path) -> str | None:
    """Return the ``status`` frontmatter field from an existing TaskNote, or None."""
    return _read_tasknote_field(path, "status")


def _read_operator_notes(path: Path) -> str:
    """Return everything below the operator-notes separator, or empty string."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
        marker = "<!-- operator notes below this line are preserved on re-sync -->"
        idx = text.find(marker)
        if idx == -1:
            return ""
        return text[idx + len(marker):]
    except Exception:
        return ""


def _content_unchanged(path: Path, new_content: str) -> bool:
    """Return True when the existing file differs from *new_content* only in the
    ``updated:`` frontmatter field (i.e. no real change warranting a disk write)."""
    if not path.exists():
        return False
    try:
        old = path.read_text(encoding="utf-8")
        _updated_re = re.compile(r"^updated: .+$", re.MULTILINE)
        return _updated_re.sub("updated: X", old) == _updated_re.sub("updated: X", new_content)
    except Exception:
        return False


def _read_tasknote_field(path: Path, field: str) -> str | None:
    """Return a single YAML frontmatter field value, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return None
        end = text.find("\n---", 3)
        if end == -1:
            return None
        fm = yaml.safe_load(text[3:end])
        if not isinstance(fm, dict):
            return None
        value = fm.get(field)
        return str(value) if value is not None else None
    except Exception:
        return None


def _patch_tasknote_done(path: Path, modified: str) -> str | None:
    """Read a TaskNote and return a new version with ``status: done`` and updated ``updated`` field."""
    try:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^status: \S+", "status: done", text, flags=re.MULTILINE)
        text = re.sub(r"^updated: .+", f"updated: {modified}", text, flags=re.MULTILINE)
        return text
    except Exception:
        return None


def _render_tasknote(
    *,
    project_slug: str,
    blocker_slug: str,
    blocker_text_short: str,
    severity: str,
    priority: str,
    since: str,
    unblock: str,
    group: str,
    created: str,
    modified: str,
    status: str,
    due: str = "",
    operator_notes: str = "",
) -> str:
    """Render a complete TaskNote markdown file as a string."""
    card_path = f"02-projects/{group}/{project_slug}"
    project_link = f"[[{card_path}|{project_slug}]]"

    fm: dict[str, Any] = {
        "type":             "task",
        "title":            f"{project_slug}: {blocker_text_short}",
        "tags":             ["task"],
        "status":           status,
        "priority":         priority,
        "due":              due,          # operator-editable; preserved on re-sync
        "project":          f"[[{card_path}.md]]",
        "blocker_slug":     blocker_slug,
        "blocker_severity": severity,
        "since":            since,
        "context":          "kb-sync-blocker",
        "kb-sync-managed":  "true",
        "created":          created,
        "updated":          modified,
    }

    fm_yaml = yaml.dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    marker = "<!-- operator notes below this line are preserved on re-sync -->"
    body = "\n".join([
        f"---\n{fm_yaml}---",
        "",
        "## Unblock",
        "",
        unblock,
        "",
        "## Context",
        "",
        f"**Project:** {project_link}  ",
        f"**Severity:** `{severity}`  ",
        f"**Since:** {since}",
        "",
        "### Notes",
        "",
        "> Auto-generated by kb-sync. Do not edit `blocker_slug` or `kb-sync-managed`"
        " — they drive idempotent matching. Safe to edit: `status`, `due`, and"
        " operator notes below the separator.",
        "",
        "---",
        "",
        marker,
    ])
    if operator_notes:
        body = body + operator_notes
    return body


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    """CLI: kb-tasknote-write.py <project_slug> <card_json_path> <vault_root>"""
    if len(sys.argv) != 4:
        print(
            "Usage: kb-tasknote-write.py <project_slug> <card_json_path> <vault_root>",
            file=sys.stderr,
        )
        sys.exit(1)

    project_slug = sys.argv[1]
    card_json_path = Path(sys.argv[2])
    vault_root = Path(sys.argv[3])

    if not card_json_path.exists():
        print(f"error: card JSON not found: {card_json_path}", file=sys.stderr)
        sys.exit(1)

    card_data: dict[str, Any] = json.loads(card_json_path.read_text(encoding="utf-8"))
    result = write_task_notes(project_slug, card_data, vault_root)
    print(json.dumps(result, indent=2))
    if any(result.values()):
        print(
            f"[tasknotes] {project_slug}: "
            f"+{result['created']} created, "
            f"~{result['updated']} updated, "
            f"done={result['completed']}, "
            f"archived={result['archived']}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _main()
