#!/usr/bin/env python3
"""Deterministic atomic-note projector for kb-sync (ADR-0003).

Reads scout-cache JSON files (one per repo) and renders atomic Obsidian notes:
  - 04-cli-errors/cmd-<owner>-<slug>.md  per CLI entry
  - 04-cli-errors/err-<owner>-<code>-<slug>.md  per error entry
  - 03-adr/<owner>-adr-<id>-<slug>.md  per ADR entry

Idempotent: a second run with the same --date writes nothing (mtime-stable).
Never calls the clock — all date fields come from --date or the fixture data.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

GROUPMAP = {
    "1.0-dev": "launchpad",
    "1.1-dev-tools": "toolbay",
    "2.0-career": "trajectory",
    "3.0-work": "missionops",
    "5.0-home": "groundcontrol",
}


# ---------------------------------------------------------------------------
# Idempotent write: skip the write if content is byte-identical to existing file.
# Uses atomic temp+replace so a crash never leaves a partial file.
# ---------------------------------------------------------------------------

def atomic_write(path: Path, text: str) -> bool:
    """Write *text* to *path*; return True if file was changed, False if skipped."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Force LF: the vault repo is LF-normalized; default text mode would emit
    # CRLF on Windows and churn git. read_text (above + reconcile) normalizes on
    # read, so the byte-identical compare stays correct and idempotency holds.
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


_DATE_LINE_RE = re.compile(r"^(created|updated|last-seen):.*$", re.MULTILINE)


def _strip_dates(text: str) -> str:
    """Remove the three date-bearing frontmatter lines for content comparison.

    Masks `created:`, `updated:`, and `last-seen:` lines so that a re-projection
    with a new `--date` does not look like a content change when the substantive
    note content is unchanged. Only these three lines are masked — `date-decided`
    (ADR fixture date, substantive), `stale-since` (reconcile-owned), and
    `last-documented-sha` are left intact.
    """
    return _DATE_LINE_RE.sub("", text)


def write_note(path: Path, new_text: str, prior_text: str) -> bool:
    """Write *new_text* to *path* with date-aware idempotency.

    Compare the new rendered text against the existing file's text with the three
    date fields (`created`, `updated`, `last-seen`) masked out. If the non-date
    content is identical the existing file is kept byte-for-byte (no write, all
    three dates preserved unchanged). If the content differs, or the note is new,
    delegate to atomic_write which also handles the byte-identical fast-path.

    This is the projection-layer write helper. Do NOT use for reconcile/_mark_stale
    or regen_index — those paths require byte-exact comparisons without date masking.

    Args:
        path: destination vault note path
        new_text: fully rendered note text (with --date already baked in)
        prior_text: existing file content (empty string when the note is new)

    Returns:
        True if the file was written/changed, False if skipped.
    """
    if prior_text and _strip_dates(prior_text) == _strip_dates(new_text):
        # Non-date content is identical — keep existing file untouched so that
        # `created`, `updated`, and `last-seen` do not move on this run.
        return False
    return atomic_write(path, new_text)


# ---------------------------------------------------------------------------
# Render helpers — pure functions; no I/O, no clock, no random.
# Tag lists are ordered lists (not sets) to guarantee deterministic output.
# ---------------------------------------------------------------------------

_PY_EXT_RE = re.compile(r"\.(py|ipynb)$", re.IGNORECASE)
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def derive_cli_slug(command: str, fallback: str = "") -> str:
    """Deterministically derive a CLI note slug from the scout's command string (ADR-0004).

    Stable across the scout's path/extension variation: "falcon-rtr-run",
    "falcon-rtr-run.py", and "incident-response/falcon-rtr-run.py" all yield
    "falcon-rtr-run", so a re-scrape MERGES onto the existing note instead of minting
    a drifted slug. Takes the basename (handles / and \\), strips only the PYTHON
    extension (.py/.ipynb — the dominant convention in the existing corpus), then
    kebab-cases. NON-python extensions are FOLDED INTO the slug (run.ps1 -> "run-ps1")
    so a `.ps1` and a `.py` of the same stem never collide onto one note. Falls back to
    the scout slug only when the command yields nothing (a malformed command can't
    produce an empty slug).
    """
    base = str(command or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _PY_EXT_RE.sub("", base)
    slug = _NONALNUM_RE.sub("-", base.lower()).strip("-")
    return slug or fallback


def _yaml_str(value: str) -> str:
    """Wrap a string in double-quotes for YAML scalar, escaping `\\` and `"`.

    Backslash MUST be escaped first so the quote-escape's own backslash is not
    doubled. Without this, any value containing a literal `"` or `\\` (scout
    error codes/messages, ADR titles routinely do) produces unparseable YAML.
    """
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_cmd(owner: str, gslug: str, sha: str, date: str, it: dict, created: str | None = None) -> str:
    """Render a cli note from a scout 'cli' item."""
    note_title = f"cmd-{owner}-{it['slug']}"
    tags = [
        "type/cli",
        f"group/{gslug}",
        f"tool/{owner}",
    ]
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
    # Preserve the existing note's `created` date if it has one; only stamp
    # --date when the note is brand-new (no existing created value).
    created_val = created or date

    lines = [
        "---",
        "type: cli",
        f"title: {_yaml_str(note_title)}",
        f"aliases: [{_yaml_str(it['command'])}, {_yaml_str(note_title)}]",
        f"tags: {tags_yaml}",
        "status: active",
        f"created: {created_val}",
        f"updated: {date}",
        f"related: [{_yaml_str(f'[[{owner}]]')}]",
        f"up: {_yaml_str(f'[[{owner}]]')}",
        f"tool: {_yaml_str(owner)}",
        f"command: {_yaml_str(it['command'])}",
        "exit-code: 0",
        'since: ""',
        f"last-documented-sha: {_yaml_str(sha)}",
        f"last-seen: {date}",
        "stale: false",
        "---",
        "",
        f"# {note_title}",
        "",
        "## Invocation",
        "",
        "```bash",
        it.get("invocation", it["command"]),
        "```",
        "",
    ]

    flags = it.get("flags", [])
    if flags:
        lines += [
            "## Flags",
            "",
            "| Flag | Description |",
            "|---|---|",
        ]
        for f in flags:
            lines.append(f"| `{f}` | — |")
        lines.append("")

    lines += [
        "## Related",
        "",
        f"- Project: [[{owner}]]",
        "",
    ]

    return "\n".join(lines)


def render_err(owner: str, gslug: str, sha: str, date: str, it: dict, created: str | None = None) -> str:
    """Render an error note from a scout 'errors' item."""
    note_title = f"err-{owner}-{it['slug']}"
    tags = [
        "type/error",
        f"group/{gslug}",
        f"tool/{owner}",
    ]
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
    # Preserve the existing note's `created` date if it has one; only stamp
    # --date when the note is brand-new (no existing created value).
    created_val = created or date

    lines = [
        "---",
        "type: error",
        f"title: {_yaml_str(note_title)}",
        f"aliases: [{_yaml_str(it['slug'].upper())}, {_yaml_str(note_title)}]",
        f"tags: {tags_yaml}",
        "status: active",
        f"created: {created_val}",
        f"updated: {date}",
        f"related: [{_yaml_str(f'[[{owner}]]')}]",
        f"up: {_yaml_str(f'[[{owner}]]')}",
        f"tool: {_yaml_str(owner)}",
        f"code: {_yaml_str(it['code'])}",
        "exit-code: null",
        'since: ""',
        f"last-documented-sha: {_yaml_str(sha)}",
        f"last-seen: {date}",
        "stale: false",
        "---",
        "",
        f"# {note_title}",
        "",
    ]

    if it.get("message"):
        lines += [
            "## Error message",
            "",
            "```",
            it["message"],
            "```",
            "",
        ]

    if it.get("trigger"):
        lines += [
            "## Trigger",
            "",
            it["trigger"],
            "",
        ]

    if it.get("fix"):
        lines += [
            "## Fix",
            "",
            it["fix"],
            "",
        ]

    lines += [
        "## Related",
        "",
        f"- Project: [[{owner}]]",
        "",
    ]

    return "\n".join(lines)


def render_adr(owner: str, gslug: str, sha: str, date: str, it: dict, created: str | None = None) -> str:
    """Render an ADR stub note from a scout 'adrs' item."""
    note_title = f"{owner}-adr-{it['id']}-{it['slug']}"
    tags = [
        "type/adr",
        f"group/{gslug}",
        f"project/{owner}",
    ]
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"

    # date-decided comes from the fixture's own date field (the decision date),
    # NOT from --date (which is the run/observation date).
    date_decided = it.get("date", date)
    # Preserve the existing note's `created` date if it has one; only stamp
    # --date when the note is brand-new (no existing created value).
    created_val = created or date

    lines = [
        "---",
        "type: adr",
        f"title: {_yaml_str(note_title)}",
        f"aliases: [{_yaml_str(note_title)}, {_yaml_str(it['title'])}]",
        f"tags: {tags_yaml}",
        f"status: {it.get('status', 'proposed')}",
        f"created: {created_val}",
        f"updated: {date}",
        f"related: [{_yaml_str(f'[[{owner}]]')}]",
        f"up: {_yaml_str(f'[[{owner}]]')}",
        f"project: {_yaml_str(owner)}",
        f"adr-id: {_yaml_str(it['id'])}",
        'supersedes: ""',
        'superseded-by: ""',
        "deciders: []",
        f"date-decided: {_yaml_str(date_decided)}",
        f"last-documented-sha: {_yaml_str(sha)}",
        f"last-seen: {date}",
        "stale: false",
        "---",
        "",
        f"# {note_title}",
        "",
        f"**Title:** {it['title']}",
        "",
        f"**Status:** `{it.get('status', 'proposed')}`",
        "",
        f"**Project:** [[{owner}]]",
        "",
        "> [!warning] Stub only",
        "> This card holds metadata. The authoritative decision body lives in the repo.",
        "",
    ]

    # Options-compared table (Task 10): emitted ONLY when the scout harvested
    # alternatives from the repo ADR. Degrades gracefully (nothing extra) when
    # absent — most ADR stubs carry no options. NEVER invented upstream.
    # _cell escapes pipes + flattens newlines so a cell never breaks the table.
    options = it.get("options")
    if options:
        lines += [
            "## Options compared",
            "",
            "| Option | Pros | Cons | Cost |",
            "|---|---|---|---|",
        ]
        for o in options:
            name = _cell(o.get("name") or "")
            pros = _cell(o.get("pros") or "")
            cons = _cell(o.get("cons") or "")
            cost = _cell(o.get("cost") or "")
            lines.append(f"| {name} | {pros} | {cons} | {cost} |")
        lines.append("")
        rec = it.get("recommendation")
        if rec:
            # _cell (not raw): a multi-sentence recommendation may carry newlines
            # or pipes; flatten/escape them so the line never breaks rendering.
            lines += [f"**Recommendation:** {_cell(rec)}", ""]

    return "\n".join(lines)


SEVERITY_RANK = {"crit": 0, "high": 1, "med": 2, "low": 3}


def render_blocker(owner: str, gslug: str, sha: str, date: str, it: dict) -> str:
    """Render a blocker note from a scout 'blockers' item.

    One note per blocker (Bases is one-row-per-note). `severity-rank` is emitted
    as a BARE integer (no quotes) so Bases sorts blockers crit->low numerically;
    `since`/`unblock` may be null/absent -> rendered as empty strings.
    """
    note_title = f"blk-{owner}-{it['slug']}"
    sev = it["severity"]
    rank = SEVERITY_RANK.get(sev, 3)
    # `it.get(key) or ""` (NOT get(key, "")): a present-but-null field returns
    # None from get(key, "") and would render the literal "None".
    since = it.get("since") or ""
    unblock = it.get("unblock") or ""
    text = it.get("text", "")

    tags = [
        "type/blocker",
        f"group/{gslug}",
    ]
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"

    lines = [
        "---",
        "type: blocker",
        f"title: {_yaml_str(note_title)}",
        f"project: {_yaml_str(owner)}",
        f"severity: {_yaml_str(sev)}",
        f"severity-rank: {rank}",
        f"since: {_yaml_str(since)}",
        f"unblock: {_yaml_str(unblock)}",
        f"text: {_yaml_str(text)}",
        f"up: {_yaml_str(f'[[{owner}]]')}",
        f"tags: {tags_yaml}",
        f"last-documented-sha: {_yaml_str(sha)}",
        f"last-seen: {date}",
        "stale: false",
        "---",
        "",
        f"# {note_title}",
        "",
        f"> {text}",
        "",
        f"**Unblock:** {unblock or '—'}",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Projector: processes one scout JSON file
# ---------------------------------------------------------------------------

def project(scout: dict, vault: Path, date: str, with_errors_adrs: bool = True) -> set:
    """Render the atomic notes the scout currently lists; return their basenames.

    ADR-0005 layer scoping: all four layers are projected by default. CLI (`cmd-*`)
    slugs are derived DETERMINISTICALLY from the command (not the scout's slug) so
    re-scrapes merge by stable key. Errors (`err-*`) and ADRs (`*-adr-*`) are now
    projected by DEFAULT — the deterministic harvest (ADR-0005 provenance split)
    makes slugs stable, so a re-scrape is a clean accumulate-only merge and never
    bloats the layers. Pass `with_errors_adrs=False` (CLI: `--frozen-errors-adrs`)
    to opt out of err/ADR projection on a run where you explicitly want to freeze
    those layers.

    The returned set is the *fresh* set: a note is fresh because the scout still
    covers it, NOT because atomic_write wrote bytes this run. Byte-identical skips
    (idempotent re-runs) are still fresh. See ADR-0003/0004/0005.
    """
    owner = scout["name"]
    gslug = GROUPMAP.get(scout["group"], scout["group"])
    sha = scout["head_sha"]

    ce = vault / "04-cli-errors"
    adr_dir = vault / "03-adr"
    blk_dir = vault / "08-blockers"

    # Ensure target dirs exist (defensive; fixture pre-creates them but real
    # vaults may be missing a folder on first run). The 08-blockers mkdir MUST
    # be here at the top (not inline before the loop): the fixture now carries a
    # blocker, so EVERY existing test calls project() against a possibly-missing
    # 08-blockers dir. If the dir were created lazily and the blocker loop threw
    # FileNotFoundError, main()'s broad except would silently `skip` the file
    # AFTER cmd/err/adr were already written — a masked failure.
    ce.mkdir(parents=True, exist_ok=True)
    adr_dir.mkdir(parents=True, exist_ok=True)
    blk_dir.mkdir(parents=True, exist_ok=True)

    fresh: set = set()

    for it in scout.get("cli", []):
        # Deterministic slug from the command (ADR-0004) — NOT the scout's slug.
        slug = derive_cli_slug(it.get("command", ""), it.get("slug", ""))
        it_local = dict(it, slug=slug)  # render_cmd reads it['slug'] for title/aliases
        name = f"cmd-{owner}-{slug}.md"
        note_path = ce / name
        prior_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        prior_created = _fm_field(prior_text, "created") if prior_text else None
        write_note(note_path, render_cmd(owner, gslug, sha, date, it_local, created=prior_created), prior_text)
        fresh.add(name)

    # Errors + ADRs project by DEFAULT (ADR-0005): deterministic slugs (provenance
    # split) make a re-scrape a clean merge — the accumulate-only reconcile policy
    # means absent notes are never staled, so nightly --all runs are safe.
    # Pass --frozen-errors-adrs to opt out on a run where you want to freeze these
    # layers (e.g. during a partial re-harvest or debugging a specific layer).
    if with_errors_adrs:
        for it in scout.get("errors", []):
            name = f"err-{owner}-{it['slug']}.md"
            note_path = ce / name
            prior_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            prior_created = _fm_field(prior_text, "created") if prior_text else None
            write_note(note_path, render_err(owner, gslug, sha, date, it, created=prior_created), prior_text)
            fresh.add(name)

        for it in scout.get("adrs", []):
            name = f"{owner}-adr-{it['id']}-{it['slug']}.md"
            note_path = adr_dir / name
            prior_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            prior_created = _fm_field(prior_text, "created") if prior_text else None
            write_note(note_path, render_adr(owner, gslug, sha, date, it, created=prior_created), prior_text)
            fresh.add(name)

    for it in scout.get("blockers", []):
        name = f"blk-{owner}-{it['slug']}.md"
        note_path = blk_dir / name
        prior_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        write_note(note_path, render_blocker(owner, gslug, sha, date, it), prior_text)
        fresh.add(name)

    return fresh


# ---------------------------------------------------------------------------
# Reconcile pass (ADR-0003 + ADR-0005): per-LAYER absence policy.
#
# Atomic notes are vault-canonical. After every scout has been project()-ed this
# run, reconcile decides the fate of EXISTING notes — but the policy differs by
# layer (ADR-0005), because the three layers have different absence semantics:
#   - owner explicitly retired -> delete (the ONLY sanctioned removal, ALL layers)
#   - CLI (cmd-*): accumulate-only -> a slug absent from a thin scout is LEFT
#       UNTOUCHED (an incomplete scout must never stale a live command).
#   - errors (err-*) / ADRs (adr): accumulate-only -> same as CLI; deterministic
#       slugs (ADR-0005) make re-scrapes a clean merge; a thin re-scrape must not
#       stale an existing err/ADR note; only retirement removes them.
#   - blockers (blk-*): stale-on-absence -> a scouted owner's blocker that is now
#       absent IS stale-flagged (absence == resolved; clears the Bases Board).
#   - owner not scouted, not retired -> leave untouched (all layers).
# Deterministic; reuses atomic_write so re-runs are byte-stable.
# ---------------------------------------------------------------------------

def load_retired(vault: Path) -> set:
    """Read 00-meta/retired-projects.txt: one owner per line; drop blanks/#."""
    rf = vault / "00-meta" / "retired-projects.txt"
    if not rf.exists():
        return set()
    owners = set()
    for line in rf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        owners.add(line)
    return owners


def _mark_stale(text: str, date: str) -> str:
    """Set `stale: false`->`stale: true` and ensure a `stale-since:` line.

    Insert `stale-since: <date>` immediately after the stale line only if no
    such line already exists — an existing stale-since records when the note
    FIRST went stale and must not be overwritten (keeps re-runs byte-stable).
    """
    lines = text.splitlines(keepends=False)
    has_since = any(ln.startswith("stale-since:") for ln in lines)
    out = []
    for ln in lines:
        if ln.startswith("stale:"):
            out.append("stale: true")
            if not has_since:
                out.append(f"stale-since: {date}")
        else:
            out.append(ln)
    # Preserve trailing newline of the original (splitlines drops it).
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing


def reconcile(vault: Path, scouted_owners: set, fresh: set, date: str) -> None:
    retired = load_retired(vault)
    # (folder, owner-field, stale_on_absence) — ADR-0005 per-layer policy.
    # Only blockers stale-flag on absence; CLI and err/ADR are accumulate-only
    # (deterministic slugs, ADR-0005), so for those layers the ONLY mutation
    # reconcile makes is retirement. A thin re-scrape never stales a live note.
    targets = [
        (vault / "04-cli-errors", "tool", False),    # cmd-* (accumulate) + err-* (accumulate)
        (vault / "03-adr", "project", False),          # ADR (accumulate-only)
        (vault / "08-blockers", "project", True),      # blockers (stale-on-absence)
    ]
    for folder, key, stale_on_absence in targets:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.name == "_INDEX.md":
                continue
            text = path.read_text(encoding="utf-8")
            # _fm_field is bounded to the `---`-fenced block, so a hand-authored
            # body line starting `tool:`/`project:` cannot be misread as the
            # owner and wrongly trigger the retirement-delete path.
            owner = _fm_field(text, key)
            if owner is None:
                continue
            if owner in retired:
                os.remove(path)
            elif stale_on_absence and owner in scouted_owners and path.name not in fresh:
                atomic_write(path, _mark_stale(text, date))
            # else: accumulate-only / frozen / not-scouted -> leave untouched


# ---------------------------------------------------------------------------
# Index regeneration (ADR-0003): rebuild the two routing-table MOCs.
#
# Each _INDEX.md is hand-authored EXCEPT the rows between the two markers:
#   <!-- KB-SYNC:ROWS:START -->  ...generated rows...  <!-- KB-SYNC:ROWS:END -->
# We replace ONLY the inter-marker slice, preserving the shell verbatim. If the
# file is absent we create a minimal shell; if markers are absent we append them.
# Deterministic ordering + write-if-changed (atomic_write) keep re-runs byte-stable.
# ---------------------------------------------------------------------------

ROWS_START = "<!-- KB-SYNC:ROWS:START -->"
ROWS_END = "<!-- KB-SYNC:ROWS:END -->"


def _fm_field(text: str, key: str) -> str | None:
    """Pull `key: value` from the YAML frontmatter block; None if absent.

    Scans only the leading `---`-fenced block. Strips surrounding quotes.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        # No frontmatter fence — scan the whole text leniently (defensive).
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


def _first_body_line(text: str) -> str | None:
    """First meaningful content line after the H1.

    Skips frontmatter, the H1, blank lines, markdown headings (`#`), and the
    code-fence *delimiters* — but KEEPS fence *content*. Generated cmd/err notes
    carry their most descriptive line inside the fenced invocation/message block
    (the prose between H1 and the first heading is empty), so the fenced line is
    the right description; skipping fence content would land on `## Related`
    boilerplate (identical for every note of an owner). None if nothing found.
    """
    lines = text.splitlines()
    i = 0
    # Skip frontmatter block.
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1  # past closing fence
    seen_h1 = False
    in_fence = False
    for ln in lines[i:]:
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            if not s:
                continue
            return s  # fenced invocation (cmd) / message (err) — distinct & meaningful
        if not s:
            continue
        if s.startswith("# "):
            seen_h1 = True
            continue
        if s.startswith("#"):  # any other heading line
            continue
        if not seen_h1:
            continue
        return s
    return None


def _cell(value: str) -> str:
    """Sanitize a value for a markdown table cell (escape pipes, flatten newlines)."""
    return value.replace("|", r"\|").replace("\n", " ").strip()


def _replace_rows(text: str, rows_block: str) -> str:
    """Replace the slice strictly between the two markers with *rows_block*.

    Fixed-point safe: the output, re-fed, reproduces itself byte-for-byte.
    If markers are absent, append a marker block (with the rows) at the end.
    """
    si = text.find(ROWS_START)
    ei = text.find(ROWS_END)
    if si != -1 and ei != -1 and si < ei:
        i = si + len(ROWS_START)
        return text[:i] + "\n" + rows_block + "\n" + text[ei:]
    # Markers absent: append them (preserve existing content + one trailing NL).
    sep = "" if text.endswith("\n") or text == "" else "\n"
    block = f"{sep}\n{ROWS_START}\n{rows_block}\n{ROWS_END}\n"
    return text + block


def _build_rows(records: list, header: str, sep: str) -> str:
    """Assemble header + separator + data rows into a single newline-joined block."""
    return "\n".join([header, sep, *records])


def regen_index(vault: Path) -> None:
    """Rebuild 04-cli-errors/_INDEX.md and 03-adr/_INDEX.md row tables."""

    # ---- 04-cli-errors: cmd-*.md + err-*.md ----
    ce = vault / "04-cli-errors"
    if ce.exists():
        rows = []
        for path in ce.glob("*.md"):
            if path.name == "_INDEX.md":
                continue
            if not (path.name.startswith("cmd-") or path.name.startswith("err-")):
                continue
            text = path.read_text(encoding="utf-8")
            base = path.stem
            tool = _fm_field(text, "tool") or ""
            status = _fm_field(text, "status") or ""
            desc = (
                _fm_field(text, "desc")
                or _fm_field(text, "summary")
                or _first_body_line(text)
                or _fm_field(text, "title")
                or base
            )
            # sort key first, then the rendered row
            rows.append(((tool, base), f"| [[{base}]] | {_cell(desc)} | {_cell(status)} |"))
        rows.sort(key=lambda r: r[0])
        rows_block = _build_rows(
            [r[1] for r in rows], "| Note | Description | Status |", "|---|---|---|"
        )
        idx = ce / "_INDEX.md"
        if idx.exists():
            text = idx.read_text(encoding="utf-8")
        else:
            text = (
                "---\ntype: moc\n---\n\n# CLI & errors\n\n"
                f"{ROWS_START}\n{ROWS_END}\n"
            )
        atomic_write(idx, _replace_rows(text, rows_block))

    # ---- 03-adr: *-adr-*.md ----
    adr_dir = vault / "03-adr"
    if adr_dir.exists():
        rows = []
        for path in adr_dir.glob("*-adr-*.md"):
            if path.name == "_INDEX.md":
                continue
            text = path.read_text(encoding="utf-8")
            base = path.stem
            project = _fm_field(text, "project") or ""
            status = _fm_field(text, "status") or ""
            decided = _fm_field(text, "date-decided") or _fm_field(text, "date") or ""
            rows.append(
                ((project, base), f"| [[{base}]] | {_cell(status)} | {_cell(decided)} |")
            )
        rows.sort(key=lambda r: r[0])
        rows_block = _build_rows(
            [r[1] for r in rows], "| Note | Status | Decided |", "|---|---|---|"
        )
        idx = adr_dir / "_INDEX.md"
        if idx.exists():
            text = idx.read_text(encoding="utf-8")
        else:
            text = (
                "---\ntype: moc\n---\n\n# ADR stubs\n\n"
                f"{ROWS_START}\n{ROWS_END}\n"
            )
        atomic_write(idx, _replace_rows(text, rows_block))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render atomic Obsidian notes from kb-sync scout-cache JSON files."
    )
    ap.add_argument("--cache", required=True, help="Directory containing scout JSON files")
    ap.add_argument("--vault", required=True, help="Root of the Obsidian vault")
    ap.add_argument("--date", required=True, help="ISO date for created/updated/last-seen (YYYY-MM-DD)")
    ap.add_argument("--only", default=None, help="Process only this project stem (for incremental runs)")
    ap.add_argument("--frozen-errors-adrs", action="store_true",
                    help="OPT-OUT: skip (re)projection of error + ADR atomic notes this run. Default OFF: "
                         "per ADR-0005 err/ADR project by default (deterministic slugs from the provenance "
                         "split make re-scrapes a clean accumulate-only merge). Use this flag only when you "
                         "explicitly want to freeze those layers — e.g. during a partial re-harvest or "
                         "while debugging a specific layer.")
    a = ap.parse_args()

    # Validate --date shape ONLY (the sole permitted datetime use — never to
    # read the clock). Raises ValueError with a clear message on malformed input.
    import datetime
    datetime.date.fromisoformat(a.date)

    vault = Path(a.vault)
    cache_dir = Path(a.cache)

    scouted_owners: set = set()
    fresh: set = set()

    for jf in sorted(cache_dir.glob("*.json")):
        if a.only and jf.stem != a.only:
            continue
        # The per-file unit (parse + required-key access in project()) is fully
        # guarded: a valid-JSON cache missing a key must skip only THAT file, not
        # abort the run and leave reconcile + regen_index unexecuted (half-written
        # vault). Other files, reconcile, and index regen still run.
        try:
            scout = json.loads(jf.read_text(encoding="utf-8"))
            scouted_owners.add(scout["name"])
            fresh |= project(scout, vault, a.date, with_errors_adrs=not a.frozen_errors_adrs)
        except (KeyError, ValueError, Exception) as e:
            print(f"skip {jf.name}: {e}", file=sys.stderr)
            continue

    # Reconcile EXISTING notes after all scouts processed (ADR-0003):
    # delete retired owners, stale-flag orphaned notes of scouted owners,
    # leave everything else untouched.
    reconcile(vault, scouted_owners, fresh, a.date)

    # Regenerate the routing-table indexes from the final set of atomic notes
    # (after projection AND reconcile, so stale/retired changes are reflected).
    regen_index(vault)


if __name__ == "__main__":
    main()
