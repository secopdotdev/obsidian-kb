#!/usr/bin/env python3
"""Deterministic per-repo structured extractor for kb-sync (Spec §3 / D2).

Parses STRUCTURED facts from each source repo WITHOUT calling an LLM or the
network. Output is a dict matching the scout-cache JSON shape consumed by
kb-atomize.py. Task 3 implements the scaffold + three non-CLI parsers:
  harvest_identity, harvest_docs_present, harvest_adrs, harvest_structured.
CLI parsers (harvest_argparse / harvest_click_typer / harvest_make / harvest_npm)
are added in Tasks 4–5; main() is added in Task 6.

Design principles (from the spec):
  - No LLM, no network, no git shell-out in parse functions (read-only on repo).
  - Per-call try/except: a malformed file yields empty/None, never raises.
  - Deterministic: sorted outputs, stable slug derivation. Two runs → byte-identical.
  - _fm_field copied (not imported) from kb-atomize to avoid hyphen-import issues.
"""
import argparse
import ast
import importlib.util
import json
import os
import subprocess
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator

try:
    # sys.stdout/stderr are TextIOWrapper at runtime (have .reconfigure);
    # pyright sees TextIO which lacks the attribute — type: ignore is intentional.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass  # pytest capture stubs may lack reconfigure

# ---------------------------------------------------------------------------
# Source-walk filtering — never descend into vendored / build / cache dirs
# ---------------------------------------------------------------------------

# Directories never walked for source: vendored deps, build/cache, VCS, IDE.
IGNORED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", ".env", "virtualenv",
    "node_modules", "site-packages", "bower_components",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".ipynb_checkpoints", ".cache",
    "dist", "build", "target", ".next", ".nuxt", "out",
    ".idea", ".vscode",
    "anaconda_projects",
})


def iter_source_py(repo: Path) -> Iterator[Path]:
    """Yield every *.py file under *repo*, skipping ignored subtrees.

    Uses os.walk with in-place dirnames pruning so that ignored directories
    (IGNORED_DIR_NAMES or names ending in '.egg-info') are never descended
    into — even for very large virtualenvs this avoids listing thousands of
    files before filtering them.  Caller wraps in sorted() for determinism.
    """
    for dirpath, dirnames, filenames in os.walk(repo):
        # Prune ignored dirs in-place (mutating dirnames[:] stops os.walk
        # from recursing into them).
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIR_NAMES and not d.endswith(".egg-info")
        ]
        for fname in filenames:
            if fname.endswith(".py"):
                yield Path(dirpath) / fname


# ---------------------------------------------------------------------------
# Shared regex / constants
# ---------------------------------------------------------------------------

_NONALNUM_RE = re.compile(r"[^a-z0-9]+")

# Leading-number prefix in an ADR filename: e.g. "0001-foo.md" → "0001"
_ADR_ID_RE = re.compile(r"^(\d+)")

# Section headings that introduce the options block (case-insensitive)
_OPTIONS_HEADING_RE = re.compile(
    r"^#{1,3}\s+(considered\s+options|options\s+compared|options)\s*$",
    re.IGNORECASE,
)

# Recommendation / Decision line
_RECOMMENDATION_RE = re.compile(
    r"^[-*>]?\s*(recommendation|decision)\s*:\s*(.+)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# _fm_field — copied from kb-atomize.py (hyphen-import-safe; same contract)
# ---------------------------------------------------------------------------

def _fm_field(text: str, key: str) -> str | None:
    """Pull `key: value` from the YAML frontmatter block; None if absent.

    Scans only the leading `---`-fenced block. Strips surrounding quotes.
    """
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


# ---------------------------------------------------------------------------
# Slug derivation (same algorithm as kb-atomize.derive_cli_slug; re-used here
# for ADR title → slug so the two modules are consistent)
# ---------------------------------------------------------------------------

def _title_slug(title: str) -> str:
    """Deterministic kebab slug from a title: lowercase, non-alnum→'-', collapse, trim.

    Period-free by construction (period maps to '-', then collapsed).
    """
    return _NONALNUM_RE.sub("-", title.lower()).strip("-")


# ---------------------------------------------------------------------------
# harvest_identity
# ---------------------------------------------------------------------------

# Language detection: manifest filename → language label
_LANGUAGE_MANIFESTS: list[tuple[str, str]] = [
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("package.json", "javascript"),
]

# Name-extraction: manifest filename → callable(text) → str | None
def _name_from_pyproject(text: str) -> str | None:
    for ln in text.splitlines():
        m = re.match(r'^\s*name\s*=\s*["\']?([^"\'#\s]+)["\']?', ln)
        if m:
            return m.group(1).strip()
    return None


def _name_from_package_json(text: str) -> str | None:
    m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def _name_from_go_mod(text: str) -> str | None:
    for ln in text.splitlines():
        m = re.match(r"^module\s+(\S+)", ln)
        if m:
            # Use only the last path component as the project name
            return m.group(1).rstrip("/").rsplit("/", 1)[-1]
    return None


def _name_from_cargo_toml(text: str) -> str | None:
    in_package = False
    for ln in text.splitlines():
        if re.match(r"^\[package\]", ln):
            in_package = True
            continue
        if in_package and re.match(r"^\[", ln):
            break
        if in_package:
            m = re.match(r'^\s*name\s*=\s*["\']?([^"\'#\s]+)["\']?', ln)
            if m:
                return m.group(1).strip()
    return None


_NAME_EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "pyproject.toml": _name_from_pyproject,
    "setup.cfg": _name_from_pyproject,    # same `name = ...` pattern
    "go.mod": _name_from_go_mod,
    "Cargo.toml": _name_from_cargo_toml,
    "package.json": _name_from_package_json,
}


def _readme_h1(repo: Path) -> str | None:
    """First H1 from README.md (if present)."""
    for candidate in ("README.md", "readme.md", "Readme.md"):
        p = repo / candidate
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                for ln in text.splitlines():
                    m = re.match(r"^#\s+(.+)", ln)
                    if m:
                        return m.group(1).strip()
            except OSError:
                pass
    return None


def _git_field(repo: Path, *git_args: str) -> str | None:
    """Run a git command in repo; return stripped stdout or None on any error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *git_args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            return out if out else None
    except Exception:
        pass
    return None


def harvest_identity(repo: Path) -> dict:
    """Read project identity from manifest files and git metadata.

    Returns:
        {
          "name": str | None,          # from manifest or README H1
          "repo_url": str | None,      # from git remote get-url origin
          "branch": str | None,        # from git rev-parse --abbrev-ref HEAD
          "primary_binary": str | None,# deferred — parsed from scripts in Tasks 4–5
          "language": str | None,      # inferred from manifest presence
        }
    """
    name: str | None = None
    language: str | None = None
    primary_binary: str | None = None

    for manifest_name, lang_label in _LANGUAGE_MANIFESTS:
        p = repo / manifest_name
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                language = lang_label
                extractor = _NAME_EXTRACTORS.get(manifest_name)
                if extractor and name is None:
                    name = extractor(text)
                break  # stop at first manifest found
            except OSError:
                pass

    # Fall back to README H1 if manifest gave no name
    if name is None:
        name = _readme_h1(repo)

    # Strip trailing .git so vault card repo: field is a clean HTTPS URL
    raw_url = _git_field(repo, "remote", "get-url", "origin")
    repo_url = raw_url.removesuffix(".git") if raw_url else None

    # symbolic-ref works even before first commit; rev-parse handles detached HEAD
    branch = (
        _git_field(repo, "symbolic-ref", "--short", "HEAD")
        or _git_field(repo, "rev-parse", "--abbrev-ref", "HEAD")
    )

    return {
        "name": name,
        "repo_url": repo_url,
        "branch": branch,
        "primary_binary": primary_binary,
        "language": language,
    }


# ---------------------------------------------------------------------------
# harvest_docs_present
# ---------------------------------------------------------------------------

def harvest_docs_present(repo: Path) -> list[str]:
    """Return sorted bare filenames of *.md directly under <repo>/docs/kb/.

    Returns [] if the directory is absent or empty.
    """
    kb_dir = repo / "docs" / "kb"
    if not kb_dir.is_dir():
        return []
    try:
        files = sorted(
            p.name
            for p in kb_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".md"
        )
        return files
    except OSError:
        return []


# ---------------------------------------------------------------------------
# harvest_adrs — options parser
# ---------------------------------------------------------------------------

def _parse_options_block(body_lines: list[str]) -> list[dict] | None:
    """Parse a 'Considered options'/'Options' section into a list of option dicts.

    Each option is introduced by a level-3 heading (### Name).
    Bullet lines under it are matched against 'Pros:', 'Cons:', 'Cost:'.

    Returns None if the section cannot be identified or contains no items.
    NEVER invents data: if a field is missing, it is an empty string.
    """
    # Find the options section heading
    section_start: int | None = None
    for i, ln in enumerate(body_lines):
        if _OPTIONS_HEADING_RE.match(ln.strip()):
            section_start = i + 1
            break
    if section_start is None:
        return None

    # Collect lines until a sibling heading (## or #) that isn't a sub-heading
    section_lines: list[str] = []
    for ln in body_lines[section_start:]:
        # Stop at a top- or second-level heading that signals end of section
        if re.match(r"^#{1,2}\s+", ln) and not re.match(r"^#{3,}", ln):
            break
        section_lines.append(ln)

    # Parse option blocks: ### Name → bullet: - Pros: / - Cons: / - Cost:
    options: list[dict] = []
    current_name: str | None = None
    current_pros = ""
    current_cons = ""
    current_cost = ""

    def _flush(name, pros, cons, cost):
        if name:
            options.append({
                "name": name,
                "pros": pros.strip(),
                "cons": cons.strip(),
                "cost": cost.strip(),
            })

    for ln in section_lines:
        h3 = re.match(r"^###\s+(.+)", ln)
        if h3:
            _flush(current_name, current_pros, current_cons, current_cost)
            current_name = h3.group(1).strip()
            current_pros = ""
            current_cons = ""
            current_cost = ""
            continue
        # Bullet lines under an option heading
        bullet = re.match(r"^[-*]\s+(.+)", ln)
        if bullet and current_name:
            content = bullet.group(1)
            m_pros = re.match(r"(?i)pros?\s*:\s*(.*)", content)
            m_cons = re.match(r"(?i)cons?\s*:\s*(.*)", content)
            m_cost = re.match(r"(?i)cost\s*:\s*(.*)", content)
            if m_pros:
                current_pros = m_pros.group(1)
            elif m_cons:
                current_cons = m_cons.group(1)
            elif m_cost:
                current_cost = m_cost.group(1)

    _flush(current_name, current_pros, current_cons, current_cost)

    return options if options else None


def _parse_recommendation(body_lines: list[str]) -> str | None:
    """Scan body lines for a 'Recommendation:' or 'Decision:' line; return the value."""
    for ln in body_lines:
        m = _RECOMMENDATION_RE.match(ln.strip())
        if m:
            value = m.group(2).strip()
            return value if value else None
    return None


def _parse_adr_file(path: Path) -> dict | None:
    """Parse one ADR markdown file into the harvest dict shape.

    Returns None on unrecoverable error (caller skips this file).
    Fields:
        id     — from frontmatter 'adr-id'/'id', else leading digits in filename
        title  — from frontmatter 'title', else first H1 in body
        status — from frontmatter 'status' (default 'proposed')
        date   — from frontmatter 'date-decided'/'date'/'created', else None
        slug   — kebab slug from title (deterministic)
        options — list[{name, pros, cons, cost}] or None
        recommendation — str or None
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # --- id ---
    adr_id: str | None = (
        _fm_field(text, "adr-id")
        or _fm_field(text, "id")
    )
    if adr_id is None:
        m = _ADR_ID_RE.match(path.name)
        if m:
            adr_id = m.group(1)
        else:
            # No leading-digit id in filename and no ADR frontmatter id field:
            # this is likely a README, _INDEX, or template — skip it rather than
            # invent a nonsense id. Spec: NEVER invent.
            has_status = _fm_field(text, "status") is not None
            has_title = _fm_field(text, "title") is not None
            if not (has_status or has_title):
                return None
            adr_id = path.stem

    # --- title ---
    title: str | None = _fm_field(text, "title")
    if title is None:
        # First H1 in body (after frontmatter)
        lines = text.splitlines()
        in_fm = lines and lines[0].strip() == "---"
        past_fm = False
        for ln in lines[1:] if in_fm else lines:
            if not past_fm and ln.strip() == "---":
                past_fm = True
                continue
            if in_fm and not past_fm:
                continue
            m_h1 = re.match(r"^#\s+(.+)", ln)
            if m_h1:
                title = m_h1.group(1).strip()
                break

    if title is None:
        title = path.stem  # last resort — always have something

    # --- status ---
    status = _fm_field(text, "status") or "proposed"

    # --- date ---
    date: str | None = (
        _fm_field(text, "date-decided")
        or _fm_field(text, "date")
        or _fm_field(text, "created")
    )

    # --- slug ---
    slug = _title_slug(title)

    # --- body lines (after frontmatter) for options / recommendation ---
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        body_start = 1
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                body_start = i + 1
                break
    body_lines = lines[body_start:]

    # --- options ---
    options = _parse_options_block(body_lines)

    # --- recommendation ---
    recommendation = _parse_recommendation(body_lines)

    return {
        "id": adr_id,
        "slug": slug,
        "title": title,
        "status": status,
        "date": date,
        "options": options,
        "recommendation": recommendation,
    }


def harvest_adrs(repo: Path) -> list[dict]:
    """Parse ADR files from <repo>/active/decisions/*.md and <repo>/docs/adr/*.md.

    Per-file try/except: a malformed file yields no entry, never raises.
    Returns a list sorted by (id, slug) for determinism.
    """
    search_dirs = [
        repo / "active" / "decisions",
        repo / "docs" / "adr",
    ]
    results: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for adr_dir in search_dirs:
        if not adr_dir.is_dir():
            continue
        for path in sorted(adr_dir.glob("*.md")):
            try:
                entry = _parse_adr_file(path)
            except Exception:
                entry = None
            if entry is None:
                continue
            key = (entry["id"], entry["slug"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(entry)

    results.sort(key=lambda a: (a["id"], a["slug"]))
    return results


# ---------------------------------------------------------------------------
# derive_cli_slug — copied from kb-atomize.py (hyphen-import-safe; same contract)
# Strips .py/.ipynb only; folds other extensions into the slug; kebab-cases.
# ---------------------------------------------------------------------------

_PY_EXT_RE = re.compile(r"\.(py|ipynb)$", re.IGNORECASE)


def _derive_cli_slug(command: str, fallback: str = "") -> str:
    """Deterministic kebab slug from a CLI command string (mirrors kb-atomize.derive_cli_slug)."""
    base = str(command or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _PY_EXT_RE.sub("", base)
    slug = _NONALNUM_RE.sub("-", base.lower()).strip("-")
    return slug or fallback


# ---------------------------------------------------------------------------
# harvest_argparse — extract CLI entries from argparse-based Python scripts
# ---------------------------------------------------------------------------

def harvest_argparse(path: Path) -> list[dict]:
    """Parse a Python file via AST and extract argparse CLI entries.

    Finds ArgumentParser(...) usage; collects add_parser("name",...) subcommand
    entries and add_argument("--flag",...) option strings.

    Returns [{slug, command, invocation, flags[]}] — one item per subcommand
    (or one basename item when no subparsers are defined). Files with no
    ArgumentParser usage return []. Files with syntax errors return [] and log
    to stderr; they never raise.

    Flags are collected globally across all add_argument calls (not scoped per
    subparser — scoped attribution requires data-flow analysis and would add
    fragility with no AC requirement). list(dict.fromkeys(flags)) preserves
    insertion order and deduplicates, ensuring deterministic output across
    process restarts (no set() iteration).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"harvest_argparse: cannot read {path}: {exc}", file=sys.stderr)
        return []

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        print(f"harvest_argparse: syntax error in {path}: {exc}", file=sys.stderr)
        return []

    # Gate: file must contain at least one ArgumentParser(...) call.
    has_parser = False
    subcommands: list[str] = []   # names from add_parser("name", ...)
    option_strings: list[str] = []  # strings from add_argument that start with "-"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        # Detect ArgumentParser(...) as Call with func.id == "ArgumentParser"
        # OR func.attr == "ArgumentParser" (i.e. argparse.ArgumentParser).
        func_name = ""
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr

        if func_name == "ArgumentParser":
            has_parser = True
            continue

        if func_name == "add_parser":
            # First positional arg is the subcommand name (string literal).
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                subcommands.append(node.args[0].value)
            continue

        if func_name == "add_argument":
            # Collect any positional string literal args starting with "-".
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-"):
                    option_strings.append(arg.value)
            continue

    if not has_parser:
        return []

    # Deduplicate flags while preserving insertion order (no set() — hash-randomized).
    flags = list(dict.fromkeys(option_strings))
    script_name = path.name

    if subcommands:
        items = []
        for cmd in subcommands:
            slug = _derive_cli_slug(cmd)
            items.append({
                "slug": slug,
                "command": cmd,
                "invocation": f"python {script_name} {cmd}",
                "flags": flags,
            })
        return items
    else:
        # No subparsers — emit one item for the script itself.
        # command = script basename (raw); slug derived separately so the raw
        # name (e.g. "cli_argparse.py") is preserved in the command field while
        # the slug is the clean kebab form (e.g. "cli-argparse").
        command = path.stem  # filename without extension, e.g. "cli_argparse"
        slug = _derive_cli_slug(command) or command
        return [{
            "slug": slug,
            "command": command,
            "invocation": f"python {script_name}",
            "flags": flags,
        }]


# ---------------------------------------------------------------------------
# harvest_click_typer — extract CLI entries from click/typer-decorated functions
# ---------------------------------------------------------------------------

def harvest_click_typer(path: Path) -> list[dict]:
    """Parse a Python file via AST and extract click/typer CLI command entries.

    Finds functions decorated with @click.command, @click.group,
    @<app>.command, or @<grp>.command (any decorator whose .attr is
    'command' or 'group'). For each, collects @click.option / @click.argument
    string literals as flags.

    Command name: the explicit name= kwarg or first positional string in the
    decorator call; else function name with underscores replaced by hyphens.

    Returns [{slug, command, invocation, flags[]}].  Files with no click
    decorators return [].  Files with syntax errors return [] + stderr log.

    Flags list uses list(dict.fromkeys(...)) for deterministic insertion-order
    dedup — never raw set() iteration.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"harvest_click_typer: cannot read {path}: {exc}", file=sys.stderr)
        return []

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        print(f"harvest_click_typer: syntax error in {path}: {exc}", file=sys.stderr)
        return []

    script_name = path.name
    items: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        # Check decorators for a command/group decorator.
        command_name: str | None = None
        option_strings: list[str] = []

        for dec in node.decorator_list:
            # Resolve the decorator's attribute name and whether it's a Call.
            dec_attr: str | None = None
            dec_call_node: ast.Call | None = None

            if isinstance(dec, ast.Call):
                dec_call_node = dec
                inner = dec.func
                if isinstance(inner, ast.Attribute):
                    dec_attr = inner.attr
                elif isinstance(inner, ast.Name):
                    dec_attr = inner.id
            elif isinstance(dec, ast.Attribute):
                dec_attr = dec.attr
            elif isinstance(dec, ast.Name):
                dec_attr = dec.id

            if dec_attr in {"command", "group"} and command_name is None:
                # Extract explicit command name from decorator args/kwargs if any.
                if dec_call_node is not None:
                    # First positional string arg.
                    if dec_call_node.args and isinstance(dec_call_node.args[0], ast.Constant) and isinstance(dec_call_node.args[0].value, str):
                        command_name = dec_call_node.args[0].value
                    else:
                        # Look for name= kwarg.
                        for kw in dec_call_node.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                command_name = kw.value.value
                                break
                # Default: function name with underscores replaced by hyphens.
                if command_name is None:
                    command_name = node.name.replace("_", "-")

            elif dec_attr in {"option", "argument"} and dec_call_node is not None:
                # Collect positional string literal args starting with "-".
                for arg in dec_call_node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-"):
                        option_strings.append(arg.value)

        if command_name is None:
            continue  # not a click command function

        flags = list(dict.fromkeys(option_strings))
        slug = _derive_cli_slug(command_name)
        items.append({
            "slug": slug,
            "command": command_name,
            "invocation": f"python {script_name} {command_name}",
            "flags": flags,
        })

    return items


# ---------------------------------------------------------------------------
# harvest_make — extract targets from a Makefile
# ---------------------------------------------------------------------------

# Regex for a valid Make target line: starts with [a-zA-Z], followed by
# alphanumerics / underscores / hyphens / dots, then a colon that is NOT
# immediately followed by '=' (which would make it a := variable assignment).
# The (?:[^=]|$) handles both "target: ..." and bare "target:" (end of line).
_MAKE_TARGET_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_.-]*)(?:::|:)(?:[^=]|$)")
# Also match the trailing-comment form: target:  ## doc text
_MAKE_TARGET_COMMENT_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_.-]*)(?:::|:)[^=]*##\s*(.+)")
# Standalone doc-comment line directly above a target
_MAKE_DOC_COMMENT_RE = re.compile(r"^##\s+(.+)")
# Makefile candidates in GNU precedence order (first match wins)
_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")


def harvest_make(repo: Path) -> list[dict]:
    """Parse Makefile targets from <repo>/Makefile (or makefile/GNUmakefile).

    One item per real target — excludes .PHONY/.DEFAULT and similar dot-prefixed
    pseudo-targets (the ^[a-zA-Z] anchor rejects them), and excludes variable
    assignments (`:=`).

    Each item shape:
        {
          "slug": "make-<target>",
          "command": "make <target>",
          "invocation": "make <target>",
          "flags": [],
          "summary": "<doc comment or ''>",
        }

    Doc comment: a `## ` line immediately preceding the target line, OR a
    trailing `## comment` on the target line itself. Inline comment takes
    priority (both forms on the same line counts as inline).

    Returns [] if no Makefile is found or on read error (logs to stderr).
    """
    makefile: Path | None = None
    # Use set of resolved paths to guard against Windows case-insensitive
    # filesystem returning the same file for multiple candidates.
    seen_paths: set[Path] = set()
    for candidate in _MAKEFILE_NAMES:
        p = repo / candidate
        if p.exists():
            resolved = p.resolve()
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                makefile = p
                break  # first existing wins

    if makefile is None:
        return []

    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"harvest_make: cannot read {makefile}: {exc}", file=sys.stderr)
        return []

    lines = text.splitlines()
    items: list[dict] = []
    seen_slugs: dict[str, bool] = {}

    for i, line in enumerate(lines):
        # Check for inline trailing comment first
        m_inline = _MAKE_TARGET_COMMENT_RE.match(line)
        if m_inline:
            target = m_inline.group(1)
            summary = m_inline.group(2).strip()
        else:
            m_target = _MAKE_TARGET_RE.match(line)
            if not m_target:
                continue
            target = m_target.group(1)
            # Look at the line directly above for a ## doc comment
            summary = ""
            if i > 0:
                m_doc = _MAKE_DOC_COMMENT_RE.match(lines[i - 1])
                if m_doc:
                    summary = m_doc.group(1).strip()

        slug = f"make-{_derive_cli_slug(target, target)}"
        if slug in seen_slugs:
            continue
        seen_slugs[slug] = True

        command = f"make {target}"
        items.append({
            "slug": slug,
            "command": command,
            "invocation": command,
            "flags": [],
            "summary": summary,
        })

    return items


# ---------------------------------------------------------------------------
# harvest_npm — extract scripts from package.json
# ---------------------------------------------------------------------------

def harvest_npm(repo: Path) -> list[dict]:
    """Read npm scripts from <repo>/package.json and emit one item per script.

    Each item shape:
        {
          "slug": "npm-<key>",
          "command": "npm run <key>",
          "invocation": "npm run <key>",
        }

    Returns [] if:
      - package.json is absent
      - JSON is malformed (logs to stderr, never raises)
      - 'scripts' key is missing or not a dict

    Uses stdlib `json` only — no network, no LLM.
    """
    pkg = repo / "package.json"
    if not pkg.exists():
        return []

    try:
        text = pkg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"harvest_npm: cannot read {pkg}: {exc}", file=sys.stderr)
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"harvest_npm: malformed JSON in {pkg}: {exc}", file=sys.stderr)
        return []

    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []

    items: list[dict] = []
    for key in scripts:
        command = f"npm run {key}"
        slug = f"npm-{_derive_cli_slug(key, key)}"
        items.append({
            "slug": slug,
            "command": command,
            "invocation": command,
        })

    return items


# ---------------------------------------------------------------------------
# harvest_errors — extract exception classes and sys.exit codes from Python src
# ---------------------------------------------------------------------------

def harvest_errors(repo: Path) -> list[dict]:
    """Deterministic harvest of error/exit-code definitions from Python sources.

    Walks all *.py files under <repo> (sorted for determinism). For each file:
      - ClassDef nodes whose bases include a name ending in 'Error' or
        'Exception' are emitted as error items (code = class name).
      - sys.exit(<int-literal>) calls are emitted as exit items (code = 'exit-<n>').

    Item shape:
        {
          "slug": "<kebab of code>",
          "code": "<ClassName or exit-N>",
          "message": "<first line of class docstring or ''>",
          "trigger": "",   # deterministic — never interpreted
          "fix": "",       # deterministic — never interpreted
        }

    De-duplication: first-seen by slug (dict.fromkeys semantics, insertion order).
    Files with syntax errors are skipped (logged to stderr); never fatal.
    Returns [] if no .py files exist.
    """
    results: dict[str, dict] = {}  # slug → item (first-seen wins)

    for py_path in sorted(iter_source_py(repo)):
        try:
            text = py_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"harvest_errors: cannot read {py_path}: {exc}", file=sys.stderr)
            continue

        try:
            tree = ast.parse(text, filename=str(py_path))
        except SyntaxError as exc:
            print(f"harvest_errors: syntax error in {py_path}: {exc}", file=sys.stderr)
            continue

        for node in ast.walk(tree):
            # --- exception class definitions ---
            if isinstance(node, ast.ClassDef):
                is_error_class = False
                for base in node.bases:
                    base_name = ""
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr
                    if base_name.endswith("Error") or base_name.endswith("Exception"):
                        is_error_class = True
                        break
                if not is_error_class:
                    continue

                code = node.name
                slug = _title_slug(code)
                if slug in results:
                    continue

                # First line of the class docstring (ast.get_docstring handles
                # Constant string as first statement; returns None if absent)
                docstring = ast.get_docstring(node)
                message = docstring.splitlines()[0].strip() if docstring else ""

                results[slug] = {
                    "slug": slug,
                    "code": code,
                    "message": message,
                    "trigger": "",
                    "fix": "",
                }

            # --- sys.exit(<int-literal>) calls ---
            elif isinstance(node, ast.Call):
                func = node.func
                # Match sys.exit(n) where func is an Attribute: sys.exit
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "exit"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "sys"
                ):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, int)):
                    continue

                n = arg.value
                code = f"exit-{n}"
                slug = code  # already kebab; _title_slug("exit-2") == "exit-2"
                if slug in results:
                    continue

                results[slug] = {
                    "slug": slug,
                    "code": code,
                    "message": "",
                    "trigger": "",
                    "fix": "",
                }

    return list(results.values())


# ---------------------------------------------------------------------------
# harvest_gates — repo gate harvest (inline markers + artifact frontmatter)
# ---------------------------------------------------------------------------

# Lazy singleton: loaded once on first call to _get_kb_graph_mod().
_KB_GRAPH_MOD: object = None  # type: ignore[assignment]


def _get_kb_graph_mod():
    """Return the kb-graph module, loading it once via importlib (hyphen-safe).

    Singleton: the module is exec_module'd at most once per process.
    Both _get_parse_inline_gates (for harvest_gates) and _lookup_lineage
    (for lineage harvest) route through here so the module is never double-loaded.
    """
    global _KB_GRAPH_MOD
    if _KB_GRAPH_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "kb_graph", Path(__file__).with_name("kb-graph.py")
        )
        assert spec is not None and spec.loader is not None, "kb-graph.py not found"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _KB_GRAPH_MOD = mod
    return _KB_GRAPH_MOD


def _get_parse_inline_gates() -> "Callable[[str], tuple[list[dict], int]]":
    """Return _parse_inline_gates from kb-graph.py (loaded once via importlib).

    kb-graph.py has a hyphenated filename so normal import doesn't work.
    This mirrors the same lazy-loader pattern used in kb-index.py.
    Delegates to _get_kb_graph_mod() so the module is exec'd at most once.
    """
    return getattr(_get_kb_graph_mod(), "_parse_inline_gates")  # type: ignore[return-value]


# Directory names to skip when walking repo trees for gate scanning.
# .planning is GSD-owned (never our files); standard IGNORED_DIR_NAMES also apply.
_GATE_SCAN_SKIP = IGNORED_DIR_NAMES | frozenset({".planning"})


def _iter_md_in_dirs(repo: Path, subdirs: list[str]) -> Iterator[Path]:
    """Yield all *.md files under repo/<subdir> for each name in subdirs.

    Skips _GATE_SCAN_SKIP directory names and non-existent subdirs.
    Sorted for determinism.
    """
    for subdir_name in subdirs:
        root = repo / subdir_name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _GATE_SCAN_SKIP
            )
            for fname in sorted(filenames):
                if fname.endswith(".md"):
                    yield Path(dirpath) / fname


def _parse_gate_list_fm(raw: str) -> list[str]:
    """Parse a YAML gate list value from frontmatter: '[a, b]' or 'a' → list.

    Handles inline lists and single bare values. Used for artifact gate `gates`
    and `requires` frontmatter fields (not the inline marker syntax).
    """
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        items = [s.strip().strip('"').strip("'") for s in inner.split(",")]
        return [s for s in items if s]
    # Bare single value
    val = raw.strip('"').strip("'")
    return [val] if val else []


def _parse_criteria_from_text(text: str) -> list[str]:
    """Extract criteria list items from YAML frontmatter block-list field.

    Handles:
        criteria:
          - "Item one"
          - "Item two"
    Returns strings only (no dict sub-items). Empty list if absent.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    # Find end of frontmatter
    fm_lines: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        fm_lines.append(ln)

    in_criteria = False
    results: list[str] = []
    for ln in fm_lines:
        stripped = ln.strip()
        if stripped == "criteria:" or stripped.startswith("criteria: "):
            in_criteria = True
            # Check for inline list on same line
            same_line = stripped[len("criteria:"):].strip()
            if same_line.startswith("[") and same_line.endswith("]"):
                inner = same_line[1:-1]
                for item in inner.split(","):
                    v = item.strip().strip('"').strip("'")
                    if v:
                        results.append(v)
                in_criteria = False  # inline — done
            continue
        if in_criteria:
            if stripped.startswith("-"):
                val = stripped[1:].strip().strip('"').strip("'")
                if val:
                    results.append(val)
            elif stripped and not stripped.startswith("#"):
                # Non-list line signals end of block
                in_criteria = False
    return results


def harvest_gates(repo: Path) -> list[dict]:
    """Scan a repo for DECLARED decision gates; return a list of gate dicts.

    Two sources are scanned:

    1. **Artifact gates** — `repo/active/gates/*.md` files with YAML frontmatter
       containing `type: gate`.  Fields extracted: gate-id, status, blocking,
       gates (list), requires (list), criteria (list), title.
       Each is normalised to the inline-gate dict shape with source="artifact".

    2. **Inline gate markers** — `<!-- @gate id=X ... -->` markers in any *.md
       file under `repo/active/` or `repo/docs/`.  Parsed by reusing
       `_parse_inline_gates` from kb-graph.py (the canonical parser).
       Source tag = "inline".

    Gate-id dedup: first occurrence wins (artifact gates are scanned first so
    that the richer frontmatter data takes precedence over inline markers).

    Ignores `.planning/` and standard IGNORED_DIR_NAMES trees.
    Malformed files are skipped (per-call try/except) and logged to stderr.

    Returns a list of dicts, each with keys:
        id (str), status (str), blocking (bool), gates (list[str]),
        requires (list[str]), criteria (list[str]), source (str),
        title (str | None), ref (str | None).
    Sorted by id for determinism.
    """
    try:
        parse_inline_gates = _get_parse_inline_gates()
    except Exception as exc:
        print(f"harvest_gates: cannot load kb-graph.py: {exc}", file=sys.stderr)
        return []

    seen_ids: set[str] = set()
    results: list[dict] = []

    # --- Pass 1: artifact gate files (active/gates/*.md) ---
    gates_dir = repo / "active" / "gates"
    if gates_dir.is_dir():
        try:
            md_files = sorted(p for p in gates_dir.iterdir() if p.suffix == ".md")
        except OSError:
            md_files = []

        for md_path in md_files:
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"harvest_gates: cannot read {md_path}: {exc}", file=sys.stderr)
                continue

            # Only harvest files explicitly typed as gates
            note_type = _fm_field(text, "type")
            if note_type != "gate":
                continue

            gate_id = _fm_field(text, "gate-id")
            if not gate_id:
                print(
                    f"harvest_gates: skip {md_path.name}: missing gate-id",
                    file=sys.stderr,
                )
                continue
            if gate_id in seen_ids:
                continue

            status = _fm_field(text, "status") or "open"
            blocking_raw = _fm_field(text, "blocking") or "false"
            blocking = blocking_raw.lower() in ("true", "1", "yes")
            title = _fm_field(text, "title")

            # `gates` scalar or list field
            gates_raw = _fm_field(text, "gates") or ""
            gates_list = _parse_gate_list_fm(gates_raw)

            # `requires` inline list (e.g. `requires: [external-crypto-audit]`)
            # _fm_field returns None for inline-list fields because they start
            # with `[`; parse directly.
            requires_list: list[str] = []
            for ln in text.splitlines():
                stripped = ln.strip()
                if stripped.startswith("requires:"):
                    raw_req = stripped[len("requires:"):].strip()
                    requires_list = _parse_gate_list_fm(raw_req)
                    break

            criteria = _parse_criteria_from_text(text)

            seen_ids.add(gate_id)
            results.append({
                "id": gate_id,
                "status": status,
                "blocking": blocking,
                "gates": gates_list,
                "requires": requires_list,
                "criteria": criteria,
                "source": "artifact",
                "title": title,
                "ref": None,
            })

    # --- Pass 2: inline markers in active/** and docs/** ---
    for md_path in _iter_md_in_dirs(repo, ["active", "docs"]):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"harvest_gates: cannot read {md_path}: {exc}", file=sys.stderr)
            continue

        # Fast skip if sigil is absent (same pattern as _collect_gates in kb-graph.py)
        if "<!-- @gate " not in text:
            continue

        try:
            gates, _skipped = parse_inline_gates(text)
        except Exception as exc:
            print(
                f"harvest_gates: _parse_inline_gates failed on {md_path}: {exc}",
                file=sys.stderr,
            )
            continue

        for g in gates:
            gid = g["id"]
            if gid in seen_ids:
                continue
            seen_ids.add(gid)
            results.append({
                "id": gid,
                "status": g.get("status", "open"),
                "blocking": g.get("blocking", False),
                "gates": g.get("gates", []),
                "requires": g.get("requires", []),
                "criteria": g.get("criteria", []),
                "source": "inline",
                "title": None,
                "ref": g.get("ref"),
            })

    return sorted(results, key=lambda g: g["id"])


# ---------------------------------------------------------------------------
# _assemble_cli — gather + normalize CLI items from all parsers for one repo
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Lineage helpers — reads advances/phase/milestones from the sidecar via kb-graph
# ---------------------------------------------------------------------------


def _lookup_lineage(vault_root: Path, name: str, identity: dict) -> dict:
    """Return {'advances', 'phase', 'milestones'} for this project from the sidecar.

    Keyed by bare project name (== --name == scout-cache basename), exact then
    case-insensitive. Missing project → documented defaults. Never raises (lineage
    is best-effort).
    """
    default: dict = {"advances": None, "phase": None, "milestones": []}
    try:
        sc = _get_kb_graph_mod()._read_sidecar(vault_root)
    except Exception as exc:
        print(f"kb-harvest: lineage lookup failed: {exc}", file=sys.stderr)
        return default
    # sidecar key convention: bare project name (== --name == scout-cache basename)
    entry = sc.get(name)
    if entry is None:
        entry = {k.lower(): v for k, v in sc.items()}.get(name.lower())
    if entry is None:
        return default
    # identity reserved for future title-based fallback; current convention keys on name
    return {
        "advances": entry.get("advances"),
        "phase": entry.get("phase"),
        "milestones": entry.get("milestones", []),
    }


# ---------------------------------------------------------------------------
# harvest_artifacts — deterministic artifact inventory
# ---------------------------------------------------------------------------

def harvest_artifacts(repo: Path) -> dict:
    """Stat-check key operator artifacts in a repo root.

    Returns:
        {
          "readme_index_exists": bool,   # .readme/_index.md present
          "plan_file_exists":    bool,   # active/plan/PLAN.md present
          "decision_count":      int,    # count of *.md in active/decisions/
        }

    No reads, no parsing — stat-only.  Never raises.
    """
    try:
        readme_index = (repo / ".readme" / "_index.md").exists()
    except OSError:
        readme_index = False

    try:
        plan_file = (repo / "active" / "plan" / "PLAN.md").exists()
    except OSError:
        plan_file = False

    try:
        decisions_dir = repo / "active" / "decisions"
        if decisions_dir.is_dir():
            decision_count = len(list(decisions_dir.glob("*.md")))
        else:
            decision_count = 0
    except OSError:
        decision_count = 0

    return {
        "readme_index_exists": readme_index,
        "plan_file_exists": plan_file,
        "decision_count": decision_count,
    }


# Structured keys that main() owns and writes; all other keys are prose and
# must be preserved from the existing cache file unchanged.
_STRUCTURED_KEYS = frozenset({
    "name", "group", "head_sha",
    "identity", "docs_present", "adrs",
    "cli", "errors", "gates", "harvest_counts",
    "advances", "phase", "milestones",
    "artifacts",
})


def _assemble_cli(repo: Path) -> list[dict]:
    """Collect CLI items from all parsers; normalize flags; dedup by slug.

    Per-parser isolation: any parser that raises is logged to stderr and
    contributes an empty list — the run continues.

    Repo-relative invocation: for Python parsers (argparse, click), the
    script path embedded in `invocation` is rewritten from the bare basename
    to its POSIX-format path relative to `repo`.  Make and npm invocations
    do not embed repo-local paths, so they are left unchanged.

    Dedup: first-seen by slug wins (insertion order preserved — no set()).
    Flags: every item is normalised to carry a `flags` key (list); items
    from parsers that omit it (harvest_npm) get `flags: []`.
    """
    # Each entry: (source_tag, py_path_or_none, raw_item)
    items: list[tuple[str, Path | None, dict]] = []

    # --- Python source files: argparse + click/typer ---
    for py_path in sorted(iter_source_py(repo)):
        # argparse
        try:
            for item in harvest_argparse(py_path):
                items.append(("py", py_path, item))
        except Exception as exc:
            print(
                f"_assemble_cli: harvest_argparse raised on {py_path}: {exc}",
                file=sys.stderr,
            )
        # click/typer
        try:
            for item in harvest_click_typer(py_path):
                items.append(("py", py_path, item))
        except Exception as exc:
            print(
                f"_assemble_cli: harvest_click_typer raised on {py_path}: {exc}",
                file=sys.stderr,
            )

    # --- Makefile ---
    try:
        for item in harvest_make(repo):
            items.append(("make", None, item))
    except Exception as exc:
        print(f"_assemble_cli: harvest_make raised: {exc}", file=sys.stderr)

    # --- npm ---
    try:
        for item in harvest_npm(repo):
            items.append(("npm", None, item))
    except Exception as exc:
        print(f"_assemble_cli: harvest_npm raised: {exc}", file=sys.stderr)

    # Dedup by slug (first-seen wins) and normalize flags + invocation
    seen_slugs: dict[str, bool] = {}
    result: list[dict] = []
    for source, py_path, raw in items:
        slug = raw.get("slug", "")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs[slug] = True

        item = dict(raw)  # shallow copy — safe since values are scalars/lists

        # Normalize flags (npm/make may omit it)
        item.setdefault("flags", [])

        # Rewrite Python invocation to repo-relative POSIX path
        if source == "py" and py_path is not None:
            try:
                rel = py_path.relative_to(repo).as_posix()
                # invocation is "python <basename> [subcmd]"
                # replace only the basename portion (first occurrence)
                item["invocation"] = item["invocation"].replace(
                    py_path.name, rel, 1
                )
            except ValueError:
                # path not under repo (shouldn't happen but be safe)
                pass

        result.append(item)

    return result


# ---------------------------------------------------------------------------
# harvest_structured — assembles ALL structured keys
# ---------------------------------------------------------------------------

def harvest_structured(
    repo: Path,
    name: str,
    group: str,
    head_sha: str,
) -> dict:
    """Assemble the full structured harvest output for one repo.

    Returns a dict matching the scout-cache JSON shape:
        {
          "name": name,
          "group": group,
          "head_sha": head_sha,
          "identity": <harvest_identity(repo)>,
          "docs_present": <harvest_docs_present(repo)>,
          "adrs": <harvest_adrs(repo)>,
          "cli": [...],         # all CLI items, deduped + normalized
          "errors": [...],      # all error/exit-code items
          "harvest_counts": {"cli": N, "errors": N, "adrs": N},
        }

    Per-parser isolation: each parser call is wrapped in try/except; a
    failure logs to stderr and yields an empty result for that facet.
    """
    # Non-CLI facets
    try:
        identity = harvest_identity(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_identity raised: {exc}", file=sys.stderr)
        identity = {}

    try:
        docs_present = harvest_docs_present(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_docs_present raised: {exc}", file=sys.stderr)
        docs_present = []

    try:
        adrs = harvest_adrs(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_adrs raised: {exc}", file=sys.stderr)
        adrs = []

    # CLI facet (per-parser isolation handled inside _assemble_cli)
    try:
        cli = _assemble_cli(repo)
    except Exception as exc:
        print(f"harvest_structured: _assemble_cli raised: {exc}", file=sys.stderr)
        cli = []

    # Errors facet
    try:
        errors = harvest_errors(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_errors raised: {exc}", file=sys.stderr)
        errors = []

    # Gates facet (declared decision gates — artifact + inline)
    try:
        gates = harvest_gates(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_gates raised: {exc}", file=sys.stderr)
        gates = []

    # Artifact inventory (stat-only: .readme, plan, ADR count)
    try:
        artifacts = harvest_artifacts(repo)
    except Exception as exc:
        print(f"harvest_structured: harvest_artifacts raised: {exc}", file=sys.stderr)
        artifacts = {"readme_index_exists": False, "plan_file_exists": False, "decision_count": 0}

    return {
        "name": name,
        "group": group,
        "head_sha": head_sha,
        "identity": identity,
        "docs_present": docs_present,
        "adrs": adrs,
        "cli": cli,
        "errors": errors,
        "gates": gates,
        "artifacts": artifacts,
        "harvest_counts": {
            "cli": len(cli),
            "errors": len(errors),
            "adrs": len(adrs),
            "gates": len(gates),
        },
    }


# ---------------------------------------------------------------------------
# main() — CLI entry point (Task 6)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Parse CLI args, run harvest_structured, merge with existing cache, write atomically.

    Usage:
        py -3 kb-harvest.py --repo <path> --name <n> --group <g> \\
                             --head-sha <sha> --cache <dir>

    Reads <cache>/<name>.json if present (existing prose keys are PRESERVED).
    Updates ONLY the structured keys and writes back atomically (temp + os.replace).
    JSON is sorted-keys, indent=2, UTF-8, LF line endings — byte-stable across runs.

    Returns 0 on success, 1 on fatal argument/IO error.
    """
    ap = argparse.ArgumentParser(
        description="Deterministic per-repo structured extractor for kb-sync."
    )
    ap.add_argument("--repo", required=True, help="Path to the source repository root")
    ap.add_argument("--name", required=True, help="Project name (cache file basename)")
    ap.add_argument("--group", required=True, help="Concept group (e.g. '3.0-work')")
    ap.add_argument("--head-sha", required=True, dest="head_sha", help="Current HEAD SHA")
    ap.add_argument("--cache", required=True, help="Directory to write <name>.json into")
    ap.add_argument(
        "--vault",
        default=None,
        help="Vault root for sidecar lookup (default: <cache>/../..)",
    )

    args = ap.parse_args(argv)

    repo = Path(args.repo)
    cache_dir = Path(args.cache)

    # Ensure cache dir exists
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"kb-harvest: cannot create cache dir {cache_dir}: {exc}", file=sys.stderr)
        return 1

    cache_file = cache_dir / f"{args.name}.json"

    # Load existing prose keys (if any)
    existing: dict = {}
    if cache_file.exists():
        try:
            existing = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"kb-harvest: warning — cannot parse existing {cache_file}: {exc}",
                file=sys.stderr,
            )
            existing = {}

    # Run harvest
    structured = harvest_structured(repo, args.name, args.group, args.head_sha)

    # Stamp lineage keys (advances/phase/milestones) from the sidecar via kb-graph.
    # vault_root defaults to <cache_dir>/../../ (i.e. <vault>/00-meta/scout-cache -> <vault>).
    vault_root = Path(args.vault) if args.vault else cache_dir.parent.parent
    structured.update(_lookup_lineage(vault_root, args.name, structured.get("identity", {})))

    # Merge: update ONLY structured keys; preserve all prose keys
    merged = {k: v for k, v in existing.items() if k not in _STRUCTURED_KEYS}
    merged.update(structured)

    # Atomic write: temp file in same directory + os.replace
    try:
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp", prefix=f"{args.name}-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False))
                fh.write("\n")  # trailing newline for POSIX compatibility
        except Exception:
            # If write fails, clean up the temp file before re-raising
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, cache_file)
    except OSError as exc:
        print(f"kb-harvest: cannot write {cache_file}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
