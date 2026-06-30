#!/usr/bin/env python3
"""kb-card-write.py — KB vault card renderer.

Ported from synthPrompt() in workflow.js. Provides two public functions:
    read_existing_operator_fields(card_path: Path) -> dict
    render_card(merged: dict, vault: Path, repo: dict, existing: dict) -> str

Imported by kb-sync-run.py via importlib (hyphenated filename).
No LLM calls; no network access. Pure Python + yaml.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Mission Control group classifier table (ADR-0002)
# ---------------------------------------------------------------------------
GROUPMAP: dict[str, dict[str, str]] = {
    "1.0-dev":       {"classifier": "Launchpad",      "slug": "launchpad",     "emoji": "🚀"},
    "1.1-dev-tools": {"classifier": "Tool Bay",       "slug": "toolbay",       "emoji": "🛠️"},
    "2.0-career":    {"classifier": "Trajectory",     "slug": "trajectory",    "emoji": "📈"},
    "3.0-work":      {"classifier": "Mission Ops",    "slug": "missionops",    "emoji": "🛰️"},
    "5.0-home":      {"classifier": "Ground Control", "slug": "groundcontrol", "emoji": "🏡"},
}

# Deep-doc filename → display label
DOCS_LABELS: dict[str, str] = {
    "overview.md":    "Overview",
    "architecture.md": "Architecture",
    "cli.md":         "CLI reference",
    "errors.md":      "Errors",
    "config.md":      "Config",
    "dev-loop.md":    "Dev loop",
}

# Valid blocker severity values — all are safe YAML bare scalars (no quoting needed)
VALID_SEVERITIES: frozenset[str] = frozenset({"low", "med", "high", "crit"})


# ---------------------------------------------------------------------------
# read_existing_operator_fields
# ---------------------------------------------------------------------------

def read_existing_operator_fields(card_path: Path) -> dict[str, Any]:
    """Read operator-owned fields from an existing vault card's YAML frontmatter.

    Returns a dict with keys: objective, problem, solution, nextsteps, file,
    rag_flag (from rag-flag), status, notes, next_command (from next-command).
    Non-existent keys or missing file → all None.
    """
    null_result: dict[str, Any] = {
        "objective": None,
        "problem": None,
        "solution": None,
        "nextsteps": None,
        "completed_steps": None,  # operator-curated audit trail of done nextsteps
        "file": None,
        "rag_flag": None,
        "status": None,
        "notes": None,
        "next_command": None,
        "last_documented_sha": None,
        "blocker_unblock_map": {},  # slug → operator-curated unblock text (I1)
    }
    if not card_path.exists():
        return null_result
    try:
        text = card_path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return null_result
        end = text.find("\n---", 3)
        if end == -1:
            return null_result
        fm_text = text[3:end]
        fm = yaml.safe_load(fm_text)
        if not isinstance(fm, dict):
            return null_result
        # Extract operator-curated unblock text keyed by blocker slug (I1)
        existing_blockers_raw = fm.get("blockers") or []
        existing_blocker_map: dict[str, str] = {}
        for b in existing_blockers_raw:
            if isinstance(b, dict) and b.get("slug") and b.get("unblock"):
                existing_blocker_map[b["slug"]] = b["unblock"]
        return {
            "objective":          fm.get("objective"),
            "problem":            fm.get("problem"),
            "solution":           fm.get("solution"),
            "nextsteps":          fm.get("nextsteps"),
            "completed_steps":    fm.get("completed_steps"),
            "file":               fm.get("file"),
            "rag_flag":           fm.get("rag-flag"),
            "status":             fm.get("status"),
            "notes":              fm.get("notes"),
            "next_command":       fm.get("next-command"),
            "last_documented_sha": fm.get("last-documented-sha"),
            "blocker_unblock_map": existing_blocker_map,
        }
    except Exception:
        return null_result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rag_from_blockers(blockers: list[dict[str, Any]]) -> str:
    """Derive RAG flag from blocker severities."""
    severities = {b.get("severity", "") for b in (blockers or [])}
    if "crit" in severities or "high" in severities:
        return "red"
    if "med" in severities:
        return "yellow"
    return "green"


def _blocker_severity(blockers: list[dict[str, Any]]) -> str:
    """Return the highest severity string from blockers list; '' if empty."""
    order = ["crit", "high", "med", "low"]
    severities = {b.get("severity", "") for b in (blockers or [])}
    for s in order:
        if s in severities:
            return s
    return ""


def _strip_git(url: str) -> str:
    """Strip trailing .git from a repo URL."""
    if not url:
        return url
    return url.rstrip("/").removesuffix(".git")


def _nonempty_str(v: Any) -> bool:
    return bool(v and isinstance(v, str) and v.strip())


def _nonempty_list(v: Any) -> bool:
    return bool(v and isinstance(v, list) and len(v) > 0)


def _infer_cmd_shell(cmd: str) -> str:
    """Infer shell context from a next-command string (bash / ps1 / ssh)."""
    if not cmd:
        return "bash"
    c = cmd.strip()
    ps1_signals = ("py -3", "powershell", ".ps1", "set-", "$env:", "get-", "invoke-", "write-host")
    if any(c.lower().startswith(s) for s in ps1_signals) or c.startswith("$"):
        return "ps1"
    if c.startswith("ssh "):
        return "ssh"
    return "bash"


def _fmstr(v: Any) -> str:
    """Produce a double-quoted YAML scalar string, safely escaped."""
    if v is None:
        return '""'
    s = str(v)
    # Escape backslashes first, then double quotes
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _fm_nullable(v: Any) -> str:
    """Produce 'null' for None, or a quoted YAML string otherwise."""
    if v is None:
        return "null"
    return _fmstr(v)


def _render_blockers_fm(blist: list[dict[str, Any]]) -> str:
    """Render blockers as YAML list for frontmatter."""
    if not blist:
        return "[]"
    lines: list[str] = []
    for b in blist:
        slug = b.get("slug", "")
        text = b.get("text", "")
        severity = b.get("severity", "low")
        if severity not in VALID_SEVERITIES:
            severity = "low"  # I4: reject unknown severities — all valid values are safe YAML bare scalars
        since = b.get("since")
        unblock = b.get("unblock")
        lines.append(f"  - slug: {slug}")
        lines.append(f"    text: {_fmstr(text)}")
        lines.append(f"    severity: {severity}")
        lines.append(f"    since: {_fm_nullable(since)}")
        lines.append(f"    unblock: {_fm_nullable(unblock)}")
    return "\n" + "\n".join(lines)


def _render_list_fm(lst: Any) -> str:
    """Render a list as YAML for frontmatter (indented 2 spaces)."""
    if not lst or not isinstance(lst, list):
        return "[]"
    lines: list[str] = []
    for item in lst:
        lines.append(f"  - {_fmstr(str(item))}")
    return "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# render_card
# ---------------------------------------------------------------------------

def render_card(
    merged: dict[str, Any],
    vault: Path,
    repo: dict[str, Any],
    existing: dict[str, Any],
) -> str:
    """Render a vault project card as a Markdown string.

    merged: structured harvest cache + prose from Ollama scout (merged dict)
    vault: absolute vault root path (unused in rendering but kept for signature)
    repo: repo descriptor dict {path, name, group, head_sha, path_rel?, ...}
    existing: operator-owned field dict from read_existing_operator_fields()
    """
    name: str = repo.get("name", "")
    group: str = repo.get("group", "")
    head_sha: str = repo.get("head_sha", "")
    # path_rel: prefer explicit field, else fall back to path
    path_rel: str = repo.get("path_rel") or repo.get("path", "")

    # Group metadata
    g = GROUPMAP.get(group, {
        "classifier": group,
        "slug": re.sub(r"[^a-z0-9-]", "", group.lower()),
        "emoji": "📁",
    })

    # Identity from merged structured cache
    identity: dict[str, Any] = merged.get("identity", {}) or {}
    repo_url: str = _strip_git(identity.get("repo_url", "") or "")
    branch: str = identity.get("branch", "") or ""
    source_file: str = identity.get("source_file", "") or ""
    tier_hint: str = identity.get("tier_hint", "") or ""
    primary_binary: str = identity.get("primary_binary", "") or ""

    # Lineage keys (structured, never operator-owned)
    advances: Any = merged.get("advances")
    phase: Any = merged.get("phase")
    milestones: list[Any] = merged.get("milestones") or []

    # Blockers from merged (authoritative list)
    blockers: list[dict[str, Any]] = merged.get("blockers") or []

    # Preserve operator-curated unblock text per blocker slug (I1)
    unblock_map: dict[str, str] = existing.get("blocker_unblock_map") or {}
    if unblock_map:
        merged_blockers: list[dict[str, Any]] = []
        for b in blockers:
            slug = b.get("slug", "")
            if slug in unblock_map and unblock_map[slug]:
                b = {**b, "unblock": unblock_map[slug]}
            merged_blockers.append(b)
        blockers = merged_blockers

    # Resolved operator-owned fields (existing wins > merged > default).
    # rag_flag: operator value is preserved but floored by blocker severity —
    # a project with active high/med blockers cannot display green regardless of
    # what the operator set previously (red < yellow < green in urgency order).
    _computed_rag = _rag_from_blockers(blockers)
    _operator_rag = existing.get("rag_flag") if _nonempty_str(existing.get("rag_flag")) else _computed_rag
    _rag_order = {"red": 0, "yellow": 1, "green": 2}
    _rag_flag_resolved = _operator_rag if _rag_order.get(_operator_rag, 2) <= _rag_order.get(_computed_rag, 2) else _computed_rag

    resolved: dict[str, Any] = {
        "objective":       existing.get("objective")       if _nonempty_str(existing.get("objective"))        else merged.get("objective"),
        "problem":         existing.get("problem")         if _nonempty_str(existing.get("problem"))          else merged.get("problem"),
        "solution":        existing.get("solution")        if _nonempty_str(existing.get("solution"))         else merged.get("solution"),
        "nextsteps":       existing.get("nextsteps")       if _nonempty_list(existing.get("nextsteps"))       else (merged.get("nextsteps") or []),
        "completed_steps": existing.get("completed_steps") if _nonempty_list(existing.get("completed_steps")) else [],
        "file":            existing.get("file")            if _nonempty_str(existing.get("file"))             else merged.get("file"),
        "rag_flag":        _rag_flag_resolved,
        "status":          existing.get("status")          if _nonempty_str(existing.get("status"))           else "active",
        "notes":           existing.get("notes")           if _nonempty_str(existing.get("notes"))            else "",
        "next_command":    existing.get("next_command")    if _nonempty_str(existing.get("next_command"))     else merged.get("next_command"),
    }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Tags list
    tags: list[str] = ["type/project", f'group/{g["slug"]}']
    if tier_hint:
        tags.append(f"tier/{tier_hint}")
    reuse_tags: list[str] = merged.get("reuse_tags") or []
    tags.extend(reuse_tags)

    # Aliases
    aliases: list[str] = [primary_binary] if primary_binary else []

    # -----------------------------------------------------------------------
    # Frontmatter block
    # -----------------------------------------------------------------------
    tags_inline = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
    aliases_inline = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"

    fm_lines: list[str] = [
        "---",
        "# --- generator-owned (kb-sync rewrites every sync; do not hand-edit) ---",
        "type: project",
        f"title: {_fmstr(name)}",
        f"aliases: {aliases_inline}",
        f"tags: {tags_inline}",
        f"classifier: {_fmstr(g['classifier'])}",
        f"group: {_fmstr(group)}",
        f"source-file: {_fmstr(source_file)}",
        f"repo: {_fmstr(repo_url)}",
        f"path: '{path_rel}'",
        f"branch: {_fmstr(branch)}",
        f"last-documented-sha: {_fmstr(head_sha)}",
        f"created: {_fmstr(today)}",
        f"updated: {_fmstr(today)}",
        f"last-sync: {_fmstr(today)}",
        f'up: {_fmstr(f"[[01-groups/{group}]]")}',
        "related: []",
        'docs: "docs/kb/"',
    ]

    # Optional lineage keys (omit entirely if null/absent)
    if advances is not None:
        fm_lines.append(f"advances: {_fmstr(advances)}")
    if phase is not None:
        fm_lines.append(f"phase: {_fmstr(phase)}")
    if milestones:
        fm_lines.append("milestones:")
        for m in milestones:
            if isinstance(m, dict):
                title = m.get("title", "")
                mphase = m.get("phase") or ""
                mstatus = m.get("status", "")
                fm_lines.append(f'  - "{title}|{mphase}|{mstatus}"')
            else:
                fm_lines.append(f"  - {_fmstr(str(m))}")

    # Retrieval keywords from scout (if non-empty)
    rk = merged.get('retrieval_keywords') or []
    if rk:
        keywords_inline = "[" + ", ".join(repr(str(k)) for k in rk[:5]) + "]"
        fm_lines.append(f"retrieval-keywords: {keywords_inline}")

    # Operator-owned block
    next_cmd_str: str = resolved["next_command"] or ""
    cmd_shell: str = _infer_cmd_shell(next_cmd_str) if next_cmd_str else ""
    # stale: true when the docs/kb/_meta.md SHA lags behind the current HEAD
    existing_sha: str = existing.get("last_documented_sha") or ""
    is_stale: bool = bool(existing_sha and head_sha and existing_sha != head_sha)

    # Artifact inventory from harvest_artifacts()
    artifacts: dict = merged.get("artifacts") or {}
    readme_index_exists: bool = bool(artifacts.get("readme_index_exists"))
    plan_file_exists: bool = bool(artifacts.get("plan_file_exists"))
    decision_count: int = int(artifacts.get("decision_count") or 0)

    fm_lines += [
        "# --- operator-owned ---",
        f"status: {resolved['status']}",
        f"rag-flag: {resolved['rag_flag']}",
        f"stale: {'true' if is_stale else 'false'}",
        f"readme_index_exists: {'true' if readme_index_exists else 'false'}",
        f"plan_file_exists: {'true' if plan_file_exists else 'false'}",
        f"decision_count: {decision_count}",
        f"blocker-severity: {_blocker_severity(blockers)}",
        f"blockers: {_render_blockers_fm(blockers)}",
        f"nextsteps: {_render_list_fm(resolved['nextsteps'])}",
        f"completed_steps: {_render_list_fm(resolved['completed_steps'])}",
        f"problem: {_fm_nullable(resolved['problem'])}",
        f"solution: {_fm_nullable(resolved['solution'])}",
        f"objective: {_fm_nullable(resolved['objective'])}",
        f"file: {_fm_nullable(resolved['file'])}",
        f"next-command: {_fmstr(next_cmd_str)}",
        f"next-command-shell: {_fmstr(cmd_shell) if cmd_shell else '\"\"'}",
        f"notes: {_fmstr(resolved['notes'] or '')}",
        "---",
    ]

    frontmatter = "\n".join(fm_lines)

    # -----------------------------------------------------------------------
    # Body sections
    # -----------------------------------------------------------------------
    summary: str = merged.get("summary", "") or ""
    # First sentence for the blockquote
    first_sentence = (summary.split(".")[0].strip() + ".") if summary else "Purpose not documented."

    nextsteps_list: list[str] = resolved["nextsteps"] or []
    next_command: str = resolved["next_command"] or ""

    body: list[str] = [
        f"# {name}",
        "",
        f"> {first_sentence}",
        "",
        "> [!abstract] At a glance",
        f'> **{g["emoji"]} {g["classifier"]}** · **RAG:** `INPUT[inlineSelect(option(green), option(yellow), option(red)):rag-flag]` · **Repo:** [{name}]({repo_url})',
        "",
        "## 🚦 Operator next step",
        "",
        "> [!todo] Do this next",
    ]

    if nextsteps_list:
        for i, step in enumerate(nextsteps_list):
            body.append(f"> {i + 1}. {step}")
    else:
        body.append("> needs-triage — no next steps recorded")

    bash_content = (
        f'cd "$KB_DEV_ROOT/{path_rel}"; {next_command}'
        if next_command
        else "# no single command — see the steps above or needs-triage"
    )

    body += [
        ">",
        "> ```bash",
        f"> {bash_content}",
        "> ```",
        "> _Why:_ <!-- one line -->",
        "",
        "## ⛔ Blockers",
        "",
        "| Blocker | Severity | Since | Unblock |",
        "|---|---|---|---|",
    ]

    if blockers:
        for b in blockers:
            text = b.get("text", "")
            severity = b.get("severity", "")
            since = b.get("since") or "—"
            unblock = b.get("unblock") or "—"
            body.append(f"| {text} | {severity} | {since} | {unblock} |")
    else:
        body.append("| None | | | |")

    body.append("")

    # Architecture section
    arch: Any = merged.get("architecture") or {}
    arch_summary: str = (arch.get("summary", "") if isinstance(arch, dict) else "") or ""
    docs_present: list[str] = merged.get("docs_present") or []

    body += [
        "## 🧭 Architecture (concise)",
        "",
        arch_summary if arch_summary else "<!-- Architecture not documented. -->",
        "",
    ]

    if docs_present:
        body += [
            "| Deep doc | Location |",
            "|---|---|",
        ]
        for fname in docs_present:
            label = DOCS_LABELS.get(fname, Path(fname).stem)
            body.append(f"| {label} | [{fname}]({repo_url}/blob/main/docs/kb/{fname}) |")
    else:
        body.append("_No published docs/kb yet._")

    body.append("")

    # Roadmap (omit entirely if no milestones)
    if milestones:
        body.append("## 🗺️ Roadmap")
        body.append("")
        for m in milestones:
            if isinstance(m, dict):
                title = m.get("title", "")
                mphase = m.get("phase")
                mstatus = m.get("status", "")
                done = mstatus == "done"
                check = "- [x]" if done else "- [ ]"
                line = f"{check} **{title}**"
                if mphase:
                    line += f" _({mphase})_"
                body.append(line)
        body.append("")

    # Hub Bases blocks — Key commands
    body += [
        "## ⌨️ Key commands",
        "",
        "```base",
        "filters:",
        "  and:",
        '    - \'file.hasTag("type/cli")\'',
        f'    - \'note.tool == "{name}"\'',
        "views:",
        "  - type: table",
        "    name: Commands",
        "    order:",
        "      - file.name",
        "      - note.command",
        "```",
        "",
    ]

    # Decisions / ADRs
    body += [
        "## 🔗 Decisions · ADRs",
        "",
        "```base",
        "filters:",
        "  and:",
        '    - \'note.type == "adr"\'',
        f'    - \'note.project == "{name}"\'',
        "views:",
        "  - type: table",
        "    name: ADRs",
        "    order:",
        "      - file.name",
        "      - note.status",
        '      - note["date-decided"]',
        "```",
        "",
    ]

    # Relevant tools — only tool/*, pattern/*, capability/* tags
    qualifying_tags = [
        t for t in reuse_tags
        if t.startswith(("tool/", "pattern/", "capability/"))
    ]

    body += [
        "## 🧰 Relevant tools",
        "",
        "> Claude Toolkit capabilities that share a `tool/`·`pattern/`·`capability/` tag with this project",
        "> (ADR-0002 reuse projection). kb-sync rebuilds the `or:` list below from this card's reuse tags;",
        "> an empty list is replaced with `_No shared-tag tools yet._`.",
        "",
    ]

    if qualifying_tags:
        body += [
            "```base",
            "filters:",
            "  and:",
            '    - \'file.inFolder("07-toolkit")\'',
            "    - or:",
        ]
        for tag in qualifying_tags:
            body.append(f'        - \'file.hasTag("{tag}")\'')
        body += [
            "views:",
            "  - type: table",
            "    name: RelevantTools",
            "    order:",
            "      - file.name",
            "      - note.classifier",
            "```",
        ]
    else:
        body.append("_No shared-tag tools yet._")

    body.append("")

    # Open blockers
    body += [
        "## ⛔ Open blockers",
        "",
        "```base",
        "filters:",
        "  and:",
        '    - \'note.type == "blocker"\'',
        f'    - \'note.project == "{name}"\'',
        "    - 'note.stale != true'",
        "views:",
        "  - type: table",
        "    name: Blockers",
        "    order:",
        '      - note["severity-rank"]',
        "      - note.severity",
        "      - note.since",
        "      - note.text",
        "      - note.unblock",
        "```",
        "",
    ]

    # Actions — static meta-bind-button blocks (PHANTOMPULSE security floor)
    body += [
        "## ⚡ Actions",
        "",
        "> [!info] Click-gated only (spec §6 / PHANTOMPULSE [[obsidian-actionable-plugins]]) — nothing runs on open.",
        "> Each button runs ONE visible Shell Command on click, reading this note's `local-path` (and `next-command`)",
        "> frontmatter. Stateful runs prompt for confirmation. See [[actionable-dashboard-setup]] to define the commands.",
        "",
    ]

    # "▶ Run next step" only when next_command is set (per PHANTOMPULSE spec)
    if next_command:
        body += [
            "```meta-bind-button",
            'label: "▶ Run next step"',
            "style: primary",
            "action:",
            "  type: command",
            "  command: shell-commands:execute-kb-run-in-dir",
            "```",
            "",
        ]

    body += [
        "```meta-bind-button",
        'label: "📂 Open folder"',
        "style: default",
        "action:",
        "  type: command",
        "  command: shell-commands:execute-kb-open-folder",
        "```",
        "",
        "```meta-bind-button",
        'label: "🖥 Terminal here"',
        "style: default",
        "action:",
        "  type: command",
        "  command: shell-commands:execute-kb-open-terminal",
        "```",
        "",
        "```meta-bind-button",
        'label: "📝 Edit .env (OS editor)"',
        "style: default",
        "action:",
        "  type: command",
        "  command: shell-commands:execute-kb-edit-file",
        "```",
    ]

    body_text = "\n".join(body) + "\n"
    return frontmatter + "\n\n" + body_text
