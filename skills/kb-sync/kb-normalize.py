#!/usr/bin/env python3
"""Idempotent, dry-run-default planner that normalizes a project repo's planning
artifacts to the standard: active/{plan,decisions,gates,research}/ with YAML
frontmatter and canonical file naming.

Signature: analyze_repo(repo_path) -> NormalizationPlan
CLI:       kb-normalize.py <repo> [--apply] [--json]

Default = DRY RUN (emit plan, write nothing).
--apply executes only HIGH-CONFIDENCE (deterministic) actions; needs-review items
are always proposals only.

Idempotency guarantee: for frontmatter-migration, scaffold, and move actions, a
second run on an already-normalized repo yields an empty plan. Gate-extraction is
inherently proposal-only: it does not remove the source heading so re-runs will
re-propose gate extractions — that is disclosed in the --json output.

Status vocab: draft | proposed | accepted | active | rejected | superseded | deprecated
Type vocab:   plan | adr | gate | research

No LLM, no network, no git shell-out. stdlib only.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass  # pytest capture stubs may lack reconfigure

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid status values for the planning standard.
_STATUS_VOCAB: frozenset[str] = frozenset(
    {"draft", "proposed", "accepted", "active", "rejected", "superseded", "deprecated"}
)

# Status synonyms: normalise informal / old values to vocab.
_STATUS_NORMALISE: dict[str, str] = {
    "in-progress": "active",
    "in progress": "active",
    "wip": "draft",
    "done": "accepted",
    "closed": "accepted",
    "obsolete": "deprecated",
    "cancelled": "rejected",
    "canceled": "rejected",
}

# Directories that are never descended into during walks.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".planning",  # .planning is GSD-owned — skip always
        ".venv", "venv", "env", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "node_modules", "dist", "build", "target",
        ".idea", ".vscode",
    }
)

# Universal YAML frontmatter keys (all artifact types).
_UNIVERSAL_KEYS = ("type", "title", "status", "created", "updated", "tags", "related")

# Regex for gate-like headings (spec-specified).
_GATE_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:gate|pre-?implementation\s+gate|go/?no-?go|decision\s+gate)\b",
    re.IGNORECASE,
)

# Also catch numbered gate patterns like "### Gate 1: NetworkPolicy is enforced"
_GATE_NUMBERED_RE = re.compile(
    r"^#{1,6}\s+gate\s+\d",
    re.IGNORECASE,
)

# NOTE: "Acceptance Criteria blocks explicitly labeled a gate" (spec action 4)
# is deliberately NOT implemented — without "labeled a gate" qualifier it
# produces high FP rates (every checklist in any doc would match). The spec
# clause requires human classification. Filed as a known limitation below.

# Inline-status formats:
#   1. Markdown bold: **Status:** Accepted   **Date:** 2026-06-10
#   2. Blockquote (· separator): > Status: Draft · Date: 2026-06-10 · ...
_BOLD_STATUS_RE = re.compile(r"\*\*Status:\*\*\s*([^\s*][^*]*)", re.IGNORECASE)
_BOLD_DATE_RE = re.compile(r"\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.IGNORECASE)

# Blockquote: `> Status: X · Date: Y · ...` (any order, arbitrary extra keys)
_BLOCKQUOTE_METADATA_RE = re.compile(
    r"^>?\s*((?:[A-Za-z][A-Za-z0-9 -]*:\s*[^·\n]+(?:·\s*[A-Za-z][A-Za-z0-9 -]*:\s*[^·\n]+)*))\s*$"
)

# ADR naming: canonical = NNNN-<slug>.md (zero-padded 4 digits).
_ADR_CANON_RE = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
_ADR_ANY_NUM_RE = re.compile(r"^(?:ADR-?)?(\d+)")

# Plan naming: NN-<slug>-plan.md  OR  <slug>.md
_PLAN_CANON_RE = re.compile(r"^(?:\d{2}-)?[a-z0-9-]+-plan\.md$|^[a-z0-9-]+\.md$", re.IGNORECASE)

# Slug helpers
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Deterministic kebab slug: lowercase, non-alnum → '-', collapse, trim."""
    return _NONALNUM_RE.sub("-", text.lower()).strip("-")


def _zero4(n: int) -> str:
    """Zero-pad to 4 digits for ADR IDs."""
    return str(n).zfill(4)


# ---------------------------------------------------------------------------
# Frontmatter parsing (house idiom from kb-index.py / kb-harvest.py)
# ---------------------------------------------------------------------------

def _fm_field(text: str, key: str) -> str | None:
    """Pull `key: value` from the YAML frontmatter block; None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        body = lines
    else:
        body = []
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            body.append(ln)
    prefix = f"{key}:"
    for ln in body:
        if ln.startswith(prefix):
            return ln[len(prefix):].strip().strip('"').strip("'")
    return None


def _has_yaml_frontmatter(text: str) -> bool:
    """Return True if the text starts with a properly fenced YAML frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for ln in lines[1:]:
        if ln.strip() == "---":
            return True
    return False  # unterminated fence


def _parse_frontmatter_dict(text: str) -> dict[str, str]:
    """Parse the YAML frontmatter block into a flat dict (key → raw string value)."""
    lines = text.splitlines()
    result: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return result
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" not in ln:
            continue
        key, _, raw = ln.partition(":")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        raw = raw.strip().strip('"').strip("'")
        result[key] = raw
    return result


def _body_lines(text: str) -> list[str]:
    """Return body lines after the closing frontmatter fence (or all lines if no fence)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return lines
    past = False
    result: list[str] = []
    for ln in lines[1:]:
        if not past and ln.strip() == "---":
            past = True
            continue
        if past:
            result.append(ln)
    return result if past else lines


# ---------------------------------------------------------------------------
# Inline-metadata extraction (handles bold + blockquote formats)
# ---------------------------------------------------------------------------

def _extract_inline_metadata(text: str) -> dict[str, str]:
    """Scan the non-frontmatter text for inline status/date metadata.

    Handles two formats:
      1. Bold:      **Status:** Accepted   **Date:** 2026-06-10
      2. Blockquote: > Status: Draft · Date: 2026-06-10 · Architecture: SPEC.md

    Returns a dict with zero or more of: status, date, title.
    Called only when the file does NOT already have YAML frontmatter.
    """
    body = _body_lines(text) if _has_yaml_frontmatter(text) else text.splitlines()

    result: dict[str, str] = {}

    for ln in body:
        stripped = ln.strip()

        # --- Bold format: **Status:** X ---
        m = _BOLD_STATUS_RE.search(stripped)
        if m and "status" not in result:
            result["status"] = m.group(1).strip().rstrip("·").strip()

        m = _BOLD_DATE_RE.search(stripped)
        if m and "date" not in result:
            result["date"] = m.group(1).strip()

        # --- Blockquote / bullet format: key: val · key: val ---
        if stripped.startswith(">"):
            # Strip leading > and whitespace
            inner = stripped.lstrip(">").strip()
            # Split on '·' separator
            parts = [p.strip() for p in inner.split("·")]
            for part in parts:
                if ":" not in part:
                    continue
                k, _, v = part.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "status" and "status" not in result and v:
                    result["status"] = v
                elif k == "date" and "date" not in result and v:
                    # Accept bare dates (2026-06-10) or longer strings
                    # Extract just the ISO date portion if embedded
                    dm = re.search(r"(\d{4}-\d{2}-\d{2})", v)
                    result["date"] = dm.group(1) if dm else v

    return result


def _extract_h1(text: str) -> str | None:
    """Return the first H1 heading from the text (after frontmatter if any)."""
    lines = _body_lines(text) if _has_yaml_frontmatter(text) else text.splitlines()
    for ln in lines:
        m = re.match(r"^#\s+(.+)", ln)
        if m:
            return m.group(1).strip()
    return None


def _normalise_status(raw: str) -> str:
    """Normalise a raw status string to the vocab; fall back to 'draft'."""
    if not raw:
        return "draft"
    normalised = raw.strip().lower()
    if normalised in _STATUS_VOCAB:
        return normalised
    return _STATUS_NORMALISE.get(normalised, "draft")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

Confidence = Literal["deterministic", "needs-review"]
ActionKind = Literal[
    "frontmatter_migration",
    "adr_move",
    "plan_move",
    "gate_extraction",
    "scaffold",
]


@dataclass
class NormAction:
    kind: ActionKind
    confidence: Confidence
    source_path: str             # repo-relative POSIX path (or "" for scaffold)
    target_path: str             # repo-relative POSIX path (or "" for scaffold)
    detail: dict                 # kind-specific data (proposed_fm, criteria, etc.)
    notes: str = ""              # human-readable clarification / warning


@dataclass
class NormalizationPlan:
    repo: str
    actions: list[NormAction] = field(default_factory=list)

    # Idempotency note: gate_extraction is re-proposed on every run (source
    # headings are not removed by applying this plan).  All other action kinds
    # are self-idempotent: a second run finds frontmatter already present /
    # dirs already exist / source files already moved, and emits no action.
    idempotency_note: str = (
        "frontmatter_migration, adr_move, plan_move, scaffold: fully idempotent. "
        "gate_extraction: re-proposed each run (source heading is not removed). "
        "Run --apply only on deterministic actions; gate proposals require human review."
    )

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        return counts

    def deterministic_count(self) -> int:
        return sum(1 for a in self.actions if a.confidence == "deterministic")

    def needs_review_count(self) -> int:
        return sum(1 for a in self.actions if a.confidence == "needs-review")


# ---------------------------------------------------------------------------
# YAML frontmatter builder
# ---------------------------------------------------------------------------

def _yaml_scalar(v: str) -> str:
    """Wrap a string as a YAML double-quoted scalar (escape \\ and ")."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_proposed_frontmatter(
    type_: str,
    title: str | None,
    status: str,
    created: str | None,
    updated: str | None,
    tags: list[str] | None = None,
    related: list[str] | None = None,
) -> str:
    """Build a YAML frontmatter string for a planning artifact."""
    lines = ["---", f"type: {type_}"]
    if title:
        lines.append(f"title: {_yaml_scalar(title)}")
    lines.append(f"status: {status}")
    if created:
        lines.append(f"created: {created}")
    if updated:
        lines.append(f"updated: {updated}")
    tags_yaml = "[" + ", ".join(_yaml_scalar(t) for t in (tags or [])) + "]"
    lines.append(f"tags: {tags_yaml}")
    related_yaml = "[" + ", ".join(_yaml_scalar(r) for r in (related or [])) + "]"
    lines.append(f"related: {related_yaml}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Action detectors
# ---------------------------------------------------------------------------

def _next_adr_id(existing_ids: list[str]) -> str:
    """Compute the next 4-digit ADR id given existing string ids."""
    nums: list[int] = []
    for eid in existing_ids:
        m = re.match(r"^\d+", eid)
        if m:
            try:
                nums.append(int(m.group()))
            except ValueError:
                pass
    return _zero4(max(nums, default=0) + 1)


def _infer_type_from_path(rel_posix: str) -> str | None:
    """Infer planning artifact type from directory location."""
    parts = rel_posix.lower().split("/")
    if "decisions" in parts or "adr" in parts:
        return "adr"
    if "plan" in parts or "plans" in parts:
        return "plan"
    if "gates" in parts:
        return "gate"
    if "research" in parts:
        return "research"
    return None


def _infer_type_from_filename(name: str) -> str | None:
    """Infer artifact type from filename patterns."""
    lower = name.lower()
    if re.match(r"^\d{4}-", lower):
        return "adr"
    if lower.endswith("-plan.md") or lower == "plan.md":
        return "plan"
    if lower.startswith("adr-") or lower.startswith("adr_"):
        return "adr"
    if lower.startswith("gate-"):
        return "gate"
    return None


def detect_frontmatter_migrations(
    repo: Path,
    md_files: list[tuple[Path, str]],  # (abs_path, rel_posix)
) -> list[NormAction]:
    """Action 1: find planning markdown files lacking YAML frontmatter.

    For each candidate, proposes a YAML block with inferred type, title
    (from H1), status (from inline Status), created/updated (from inline Date).
    Files that already have a valid YAML frontmatter with a `type:` field are
    no-ops (idempotent).
    """
    actions: list[NormAction] = []

    for abs_path, rel_posix in md_files:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # --- Idempotency check: already has YAML frontmatter with type: ---
        if _has_yaml_frontmatter(text):
            fm = _parse_frontmatter_dict(text)
            if fm.get("type"):
                continue  # fully normalised — skip

        # --- Infer type ---
        inferred_type = (
            _infer_type_from_path(rel_posix)
            or _infer_type_from_filename(abs_path.name)
            or "plan"  # safest default for unknown planning docs
        )

        # --- Extract title ---
        title = _extract_h1(text)

        # --- Extract inline metadata ---
        inline = _extract_inline_metadata(text)
        raw_status = inline.get("status", "")
        status = _normalise_status(raw_status) if raw_status else "draft"
        date_val = inline.get("date")

        # --- Propose frontmatter ---
        proposed_fm = _build_proposed_frontmatter(
            type_=inferred_type,
            title=title,
            status=status,
            created=date_val,
            updated=date_val,
            tags=[],
            related=[],
        )

        notes_parts: list[str] = []
        if not title:
            notes_parts.append("no H1 found — title field will be blank")
        if not raw_status:
            notes_parts.append("no inline Status found — defaulted to 'draft'")
        if not date_val:
            notes_parts.append("no inline Date found — created/updated left blank")
        if inferred_type == "plan" and _infer_type_from_path(rel_posix) is None:
            notes_parts.append(
                "type 'plan' inferred by default; verify if file is spec/research"
            )

        actions.append(
            NormAction(
                kind="frontmatter_migration",
                confidence="needs-review",
                source_path=rel_posix,
                target_path=rel_posix,  # in-place; path doesn't change
                detail={
                    "inferred_type": inferred_type,
                    "proposed_frontmatter": proposed_fm,
                    "extracted_title": title,
                    "extracted_status": raw_status or None,
                    "extracted_date": date_val,
                },
                notes="; ".join(notes_parts) if notes_parts else "",
            )
        )

    return actions


def detect_adr_moves(repo: Path) -> list[NormAction]:
    """Action 2: ADRs in docs/adr/ or docs/superpowers/decisions/ -> active/decisions/.

    Emits git mv proposals as strings. EXCEPTION: .planning/ is never touched.
    Already-in-place (active/decisions/) ADRs are skipped.
    """
    actions: list[NormAction] = []
    target_base = repo / "active" / "decisions"

    # Existing ADR ids in active/decisions/ to avoid number collisions.
    existing_ids: list[str] = []
    if target_base.is_dir():
        for f in target_base.glob("*.md"):
            m = _ADR_ANY_NUM_RE.match(f.name)
            if m:
                existing_ids.append(m.group(1))

    search_dirs = [
        repo / "docs" / "adr",
        repo / "docs" / "superpowers" / "decisions",
    ]

    for src_dir in search_dirs:
        if not src_dir.is_dir():
            continue
        for abs_path in sorted(src_dir.glob("*.md")):
            try:
                rel_posix = abs_path.relative_to(repo).as_posix()
            except ValueError:
                continue

            # Derive canonical ADR filename.
            stem = abs_path.stem
            m_num = _ADR_ANY_NUM_RE.match(stem)
            if m_num:
                raw_num = m_num.group(1)
                # Strip leading non-digit prefix (e.g. "ADR-" or "ADR_")
                try:
                    num_int = int(raw_num)
                except ValueError:
                    num_int = len(existing_ids) + 1
                canon_id = _zero4(num_int)
            else:
                canon_id = _next_adr_id(existing_ids)
                existing_ids.append(canon_id)

            # Slug from remainder of stem after the number.
            after_num = re.sub(r"^(?:ADR-?)?(\d+)-?", "", stem, count=1, flags=re.IGNORECASE)
            slug_part = _slug(after_num) if after_num else "adr"
            canon_name = f"{canon_id}-{slug_part}.md"
            target_rel = f"active/decisions/{canon_name}"

            git_mv_cmd = f"git mv {rel_posix} {target_rel}"

            actions.append(
                NormAction(
                    kind="adr_move",
                    confidence="deterministic",
                    source_path=rel_posix,
                    target_path=target_rel,
                    detail={"git_mv": git_mv_cmd, "canon_id": canon_id, "canon_name": canon_name},
                    notes="run from repo root; verify no duplicate ADR id before applying",
                )
            )

    return actions


def detect_plan_moves(repo: Path) -> list[NormAction]:
    """Action 3: docs/superpowers/plans/ and root plan/ -> active/plan/.

    Emits git mv proposals as strings.
    """
    actions: list[NormAction] = []

    search_dirs = [
        repo / "docs" / "superpowers" / "plans",
        repo / "plan",
    ]

    for src_dir in search_dirs:
        if not src_dir.is_dir():
            continue
        for abs_path in sorted(src_dir.glob("*.md")):
            try:
                rel_posix = abs_path.relative_to(repo).as_posix()
            except ValueError:
                continue

            canon_name = abs_path.name.lower().replace(" ", "-")
            target_rel = f"active/plan/{canon_name}"
            git_mv_cmd = f"git mv {rel_posix} {target_rel}"

            actions.append(
                NormAction(
                    kind="plan_move",
                    confidence="deterministic",
                    source_path=rel_posix,
                    target_path=target_rel,
                    detail={"git_mv": git_mv_cmd},
                    notes="",
                )
            )

    return actions


def _extract_checklist_items(lines: list[str], start: int, end: int) -> list[str]:
    """Extract checklist items `- [ ]` or bullet points from a section of body lines."""
    items: list[str] = []
    for ln in lines[start:end]:
        # Markdown task: - [ ] text  or  - [x] text
        m = re.match(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)", ln)
        if m:
            items.append(m.group(1).strip())
            continue
        # Plain bullet
        m2 = re.match(r"^\s*[-*]\s+(.+)", ln)
        if m2 and not ln.strip().startswith("```"):
            items.append(m2.group(1).strip())
    return items


def detect_gate_extractions(
    repo: Path,
    md_files: list[tuple[Path, str]],
) -> list[NormAction]:
    """Action 4: detect inline gate language and propose active/gates/ stubs.

    Matches headings per the spec regex. Also matches numbered gates (Gate 1…N).
    Acceptance Criteria blocks are matched if heading text explicitly names them a gate.

    Already-proposed gates are NOT re-checked (active/gates/ is read to find existing
    gate slugs; if a matching slug already exists, skip — partial idempotency).
    """
    actions: list[NormAction] = []

    # Collect existing gate slugs so we can skip already-extracted ones.
    existing_gate_slugs: set[str] = set()
    gates_dir = repo / "active" / "gates"
    if gates_dir.is_dir():
        for gf in gates_dir.glob("gate-*.md"):
            # Extract slug portion after "gate-NNN-"
            m = re.match(r"^gate-\d+-(.+)\.md$", gf.name)
            if m:
                existing_gate_slugs.add(m.group(1))

    # Running gate counter for naming new gates.
    gate_counter = len(existing_gate_slugs) + 1

    for abs_path, rel_posix in md_files:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        body = _body_lines(text) if _has_yaml_frontmatter(text) else text.splitlines()

        i = 0
        while i < len(body):
            ln = body[i]
            is_gate = (
                _GATE_HEADING_RE.match(ln.strip())
                or _GATE_NUMBERED_RE.match(ln.strip())
            )
            if not is_gate:
                i += 1
                continue

            heading_text = re.sub(r"^#+\s*", "", ln.strip())
            gate_slug = _slug(heading_text) or "gate"

            # Skip if already extracted.
            if gate_slug in existing_gate_slugs:
                i += 1
                continue

            # Find end of this section (next same-or-higher level heading).
            heading_level = len(re.match(r"^(#+)", ln.strip()).group(1))  # type: ignore[union-attr]
            section_end = i + 1
            while section_end < len(body):
                next_ln = body[section_end].strip()
                m_h = re.match(r"^(#+)\s+", next_ln)
                if m_h and len(m_h.group(1)) <= heading_level:
                    break
                section_end += 1

            # Extract criteria items from section body.
            criteria = _extract_checklist_items(body, i + 1, section_end)

            # Build proposed gate filename.
            gate_id = str(gate_counter).zfill(3)
            gate_filename = f"gate-{gate_id}-{gate_slug}.md"
            target_path = f"active/gates/{gate_filename}"

            # Mark as already seen.
            existing_gate_slugs.add(gate_slug)
            gate_counter += 1

            # Proposed gate frontmatter.
            proposed_fm = (
                "---\n"
                f"type: gate\n"
                f"title: {_yaml_scalar(heading_text)}\n"
                "status: proposed\n"
                "gate-id: " + gate_id + "\n"
                "blocking: true\n"
                f"gates: {_yaml_scalar(rel_posix)}\n"
                "criteria:\n"
                + (
                    "".join(f"  - {_yaml_scalar(c)}\n" for c in criteria)
                    if criteria
                    else "  []\n"
                )
                + "---\n"
            )

            # Body: criteria as checkboxes.
            body_text = f"# {heading_text}\n\n"
            if criteria:
                body_text += "\n".join(f"- [ ] {c}" for c in criteria) + "\n"
            else:
                body_text += "_No extractable checklist criteria — review source._\n"

            notes_parts: list[str] = []
            if not criteria:
                notes_parts.append(
                    "no extractable checklist items found (source uses bash "
                    "'# Expected:' comments, not '- [ ]' bullets) — fill manually"
                )
            # Note: Gate headings that are verification gates (not decision gates)
            # are a known FP class for this regex — report honestly.
            notes_parts.append(
                "verify: is this a go/no-go decision gate or a verification step? "
                "The regex matches numbered '### Gate N:' headings which may be "
                "verification gates, not decision/approval gates"
            )

            actions.append(
                NormAction(
                    kind="gate_extraction",
                    confidence="needs-review",
                    source_path=rel_posix,
                    target_path=target_path,
                    detail={
                        "heading": heading_text,
                        "heading_line": i + 1,
                        "proposed_frontmatter": proposed_fm,
                        "proposed_body": body_text,
                        "criteria_count": len(criteria),
                        "criteria": criteria,
                    },
                    notes="; ".join(notes_parts),
                )
            )
            i += 1

    return actions


def detect_scaffold(repo: Path) -> list[NormAction]:
    """Action 5: if neither active/plan/ nor active/decisions/ exists, plan scaffold.

    Creates active/{plan,decisions,gates}/ with .gitkeep. Idempotent: if both
    dirs already exist, returns no actions.
    """
    plan_dir = repo / "active" / "plan"
    decisions_dir = repo / "active" / "decisions"

    if plan_dir.exists() and decisions_dir.exists():
        return []  # already scaffolded — no-op

    actions: list[NormAction] = []
    for subdir in ("plan", "decisions", "gates"):
        d = repo / "active" / subdir
        if not d.exists():
            gitkeep_rel = f"active/{subdir}/.gitkeep"
            actions.append(
                NormAction(
                    kind="scaffold",
                    confidence="deterministic",
                    source_path="",
                    target_path=gitkeep_rel,
                    detail={"create_dir": f"active/{subdir}", "create_file": gitkeep_rel},
                    notes="",
                )
            )

    return actions


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

# Directories under active/ that contain planning artifacts to scan.
_PLANNING_DIRS = (
    ("active/plan", "plan"),
    ("active/decisions", "adr"),
    ("active/research", "research"),
    ("active/gates", "gate"),
)

# Additional non-standard source directories (for move detection context).
_LEGACY_PLAN_DIRS = (
    "docs/superpowers/plans",
    "plan",
    "docs/adr",
    "docs/superpowers/decisions",
)


def _collect_planning_md(repo: Path) -> list[tuple[Path, str]]:
    """Collect all markdown files from planning directories.

    Returns (abs_path, repo_relative_posix) tuples, sorted deterministically.
    Skips _SKIP_DIRS.
    """
    files: list[tuple[Path, str]] = []

    scan_dirs = [
        repo / "active",
    ]

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(scan_dir):
            dirnames[:] = [
                d for d in dirnames if d not in _SKIP_DIRS
            ]
            dp = Path(dirpath)
            for fname in sorted(filenames):
                if not fname.lower().endswith(".md"):
                    continue
                abs_path = dp / fname
                try:
                    rel_posix = abs_path.relative_to(repo).as_posix()
                except ValueError:
                    continue
                files.append((abs_path, rel_posix))

    return sorted(files, key=lambda t: t[1])


def analyze_repo(repo_path: str | Path) -> NormalizationPlan:
    """Analyze a repo and return a NormalizationPlan (pure analysis, no writes).

    This is the public API entry point. CLI wraps it.
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo does not exist or is not a directory: {repo}")

    plan = NormalizationPlan(repo=str(repo))

    # --- Action 5: scaffold (run first — if no dirs, other actions have nothing to scan) ---
    plan.actions.extend(detect_scaffold(repo))

    # --- Collect planning MD files ---
    md_files = _collect_planning_md(repo)

    # --- Action 1: frontmatter migrations ---
    plan.actions.extend(detect_frontmatter_migrations(repo, md_files))

    # --- Action 2: ADR moves ---
    plan.actions.extend(detect_adr_moves(repo))

    # --- Action 3: plan moves ---
    plan.actions.extend(detect_plan_moves(repo))

    # --- Action 4: gate extractions ---
    plan.actions.extend(detect_gate_extractions(repo, md_files))

    return plan


# ---------------------------------------------------------------------------
# --apply (deterministic actions only)
# ---------------------------------------------------------------------------

def apply_plan(plan: NormalizationPlan, dry_run: bool = False) -> list[str]:
    """Execute only DETERMINISTIC actions in the plan.

    Currently, only 'scaffold' is executed (creates dirs + .gitkeep via atomic
    temp+replace). 'adr_move' and 'plan_move' emit the git mv command string
    but never execute it — git is out of scope for --apply.

    Returns a list of human-readable result lines.
    """
    repo = Path(plan.repo)
    results: list[str] = []

    for action in plan.actions:
        if action.confidence != "deterministic":
            continue

        if action.kind == "scaffold":
            dir_path = repo / action.detail["create_dir"]
            gitkeep_path = repo / action.detail["create_file"]
            if dry_run:
                results.append(f"[dry-run] would create {gitkeep_path}")
                continue
            dir_path.mkdir(parents=True, exist_ok=True)
            if not gitkeep_path.exists():
                # Atomic write: temp + os.replace
                fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".gitkeep-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write("")
                    os.replace(tmp, gitkeep_path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            results.append(f"created {gitkeep_path}")

        elif action.kind in ("adr_move", "plan_move"):
            cmd = action.detail.get("git_mv", "")
            results.append(
                f"[proposal — run manually] {cmd}"
            )

    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_human(plan: NormalizationPlan) -> str:
    """Render a human-readable plan report."""
    lines: list[str] = []
    summary = plan.summary()
    total = len(plan.actions)
    det = plan.deterministic_count()
    rev = plan.needs_review_count()

    lines.append(f"kb-normalize plan for: {plan.repo}")
    lines.append(f"  total actions : {total}  (deterministic: {det}  needs-review: {rev})")
    counts_str = "  ".join(f"{k}: {v}" for k, v in sorted(summary.items()))
    if counts_str:
        lines.append(f"  breakdown     : {counts_str}")
    lines.append(f"  idempotency   : {plan.idempotency_note}")
    lines.append("")

    if not plan.actions:
        lines.append("(no actions — repo is already normalized)")
        return "\n".join(lines)

    # Group by kind for readability.
    by_kind: dict[str, list[NormAction]] = {}
    for a in plan.actions:
        by_kind.setdefault(a.kind, []).append(a)

    kind_order: list[str] = [
        "scaffold",
        "frontmatter_migration",
        "adr_move",
        "plan_move",
        "gate_extraction",
    ]
    for kind in kind_order:
        if kind not in by_kind:
            continue
        actions_for_kind = by_kind[kind]
        header = kind.upper().replace("_", " ")
        lines.append(f"{'─' * 60}")
        lines.append(f"  {header}  ({len(actions_for_kind)} action(s))")
        lines.append("")

        for idx, a in enumerate(actions_for_kind, 1):
            lines.append(f"  [{idx}] confidence={a.confidence}")
            if a.source_path:
                lines.append(f"      source : {a.source_path}")
            if a.target_path and a.target_path != a.source_path:
                lines.append(f"      target : {a.target_path}")
            elif a.target_path:
                lines.append(f"      path   : {a.target_path}")

            # Kind-specific detail.
            if a.kind == "frontmatter_migration":
                lines.append("      proposed frontmatter:")
                for fm_line in a.detail["proposed_frontmatter"].splitlines():
                    lines.append(f"        {fm_line}")
            elif a.kind in ("adr_move", "plan_move"):
                lines.append(f"      git mv : {a.detail.get('git_mv', '')}")
            elif a.kind == "gate_extraction":
                lines.append(f"      heading (line {a.detail['heading_line']}): {a.detail['heading']}")
                lines.append(f"      criteria found: {a.detail['criteria_count']}")
                if a.detail["criteria"]:
                    for c in a.detail["criteria"]:
                        lines.append(f"        - [ ] {c}")
                lines.append(f"      proposed target: {a.target_path}")
            elif a.kind == "scaffold":
                lines.append(f"      create dir : {a.detail['create_dir']}")
                lines.append(f"      create file: {a.detail['create_file']}")

            if a.notes:
                lines.append(f"      notes  : {a.notes}")
            lines.append("")

    return "\n".join(lines)


def render_json(plan: NormalizationPlan) -> str:
    """Render the plan as a JSON string."""
    data = {
        "repo": plan.repo,
        "idempotency_note": plan.idempotency_note,
        "summary": plan.summary(),
        "deterministic_count": plan.deterministic_count(),
        "needs_review_count": plan.needs_review_count(),
        "actions": [asdict(a) for a in plan.actions],
    }
    return json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Usage:
        kb-normalize.py <repo_path> [--apply] [--json]

    Default: dry-run. Emit plan to stdout.
    --apply: execute deterministic actions (scaffold .gitkeep only; git mv printed, not run).
    --json:  emit JSON instead of human text.
    """
    ap = argparse.ArgumentParser(
        description="Idempotent planner: normalize a repo's planning artifacts.",
        epilog=(
            "Default = DRY RUN. --apply only executes scaffold (deterministic) actions; "
            "git mv proposals are always printed, never executed."
        ),
    )
    ap.add_argument("repo", help="Path to the repository root")
    ap.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute deterministic actions (scaffold only; git mv is always proposal-only)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output plan as JSON",
    )

    args = ap.parse_args(argv)

    try:
        plan = analyze_repo(args.repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        results = apply_plan(plan, dry_run=False)
        for r in results:
            print(r)
        print()

    if args.json:
        print(render_json(plan), end="")
    else:
        print(render_human(plan))

    return 0


if __name__ == "__main__":
    sys.exit(main())
