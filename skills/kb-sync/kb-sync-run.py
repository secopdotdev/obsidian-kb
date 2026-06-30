#!/usr/bin/env python3
"""kb-sync-run.py — zero-token KB sync pipeline orchestrator.

Reads a repos-json file, runs an Ollama prose pass per repo, writes vault
project cards, and updates the scout-cache. No Claude API tokens consumed.

Usage:
    python3 kb-sync-run.py --repos-json PATH --vault PATH
                           [--ollama-url URL] [--model NAME]
                           [--timeout-secs INT] [--dry-run]
    python3 kb-sync-run.py --repos-data JSON --vault PATH
                           [--ollama-url URL] [--model NAME]
                           [--timeout-secs INT] [--dry-run]

Exit codes:
    0 — success (or dry-run)
    1 — fatal configuration / IO error
    2 — Ollama unreachable (KB_OLLAMA_UNAVAILABLE printed to stderr)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

# ---------------------------------------------------------------------------
# Encoding safety (matches pattern in kb-harvest.py)
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# ---------------------------------------------------------------------------
# Import kb-card-write (hyphenated filename — cannot be bare-imported)
# ---------------------------------------------------------------------------
_SKILL_DIR = Path(__file__).resolve().parent
_CARD_WRITE_PATH = _SKILL_DIR / "kb-card-write.py"
_cw_spec = importlib.util.spec_from_file_location("kb_card_write", str(_CARD_WRITE_PATH))
_cw_mod = importlib.util.module_from_spec(_cw_spec)  # type: ignore[arg-type]
_cw_spec.loader.exec_module(_cw_mod)  # type: ignore[union-attr]
render_card = _cw_mod.render_card
read_existing_operator_fields = _cw_mod.read_existing_operator_fields

# ---------------------------------------------------------------------------
# Import kb-tasknote-write (lives in tools/ — hyphenated, use importlib)
# ---------------------------------------------------------------------------
_TASKNOTE_PATH = _SKILL_DIR.parent.parent / "tools" / "kb-tasknote-write.py"
_tn_spec = importlib.util.spec_from_file_location("kb_tasknote_write", str(_TASKNOTE_PATH))
_tn_mod = importlib.util.module_from_spec(_tn_spec)  # type: ignore[arg-type]
_tn_spec.loader.exec_module(_tn_mod)  # type: ignore[union-attr]
write_task_notes = _tn_mod.write_task_notes

# ---------------------------------------------------------------------------
# validate_scout — port of validateScout() from workflow.js
# ---------------------------------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Prose keys emitted by the Ollama scout pass — these are the ONLY keys prose
# is allowed to contribute to the merged dict. Structured keys (head_sha,
# identity, docs_present, etc.) must never be overridden by prose. (I3)
PROSE_KEYS: frozenset[str] = frozenset({
    "summary", "problem", "solution", "objective", "nextsteps",
    "next_command", "file", "blockers", "architecture", "reuse_tags",
    "fallback_cli", "flags", "injection_findings", "retrieval_keywords",
})

# ---------------------------------------------------------------------------
# Severity synonym normalizer
# ---------------------------------------------------------------------------

_SEV_SYNONYMS: dict[str, str] = {
    'critical': 'crit',
    'severe':   'crit',
    'medium':   'med',
    'moderate': 'med',
    'warning':  'low',
    'info':     'low',
    'low':      'low',
    'med':      'med',
    'high':     'high',
    'crit':     'crit',
}


def _norm_severity(raw: str) -> str:
    """Map Ollama severity synonyms to canonical enum values (low|med|high|crit)."""
    return _SEV_SYNONYMS.get(str(raw).lower().strip(), 'low')


def validate_scout(s: dict[str, Any]) -> tuple[bool, str]:
    """Validate Ollama prose output against the scout schema.

    Checks required prose key presence, type constraints, and blocker slug format.
    Returns (True, '') on success or (False, reason) on failure.
    """
    for key in ["summary", "nextsteps", "next_command", "blockers"]:
        if key not in s:
            return False, f"missing prose key: {key}"
    if not isinstance(s["summary"], str) or not s["summary"].strip():
        return False, "summary empty or non-string"
    if not isinstance(s["nextsteps"], list):
        return False, "nextsteps must be array"
    if not isinstance(s["blockers"], list):
        return False, "blockers must be array"
    seen: set[str] = set()
    for b in s["blockers"]:
        if not isinstance(b, dict):  # I2: guard against string/int/etc. blockers
            return False, f"blocker must be an object, got {type(b).__name__}"
        slug = b.get("slug", "")
        if not SLUG_RE.match(slug):
            return False, f"bad blocker slug: {slug}"
        if slug in seen:
            return False, f"dup blocker slug: {slug}"
        seen.add(slug)
    return True, ""


# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def _write_sentinel(path: Path) -> None:
    """Write (or overwrite) the kb-sync-active sentinel file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"started": ts, "label": "kb-sync-run"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Manifest SHA stamp
# ---------------------------------------------------------------------------

def _stamp_manifest_sha(vault: Path, name: str, head_sha: str) -> None:
    """Update last_documented_sha for `name` in kb-manifest.json (atomic write).

    The SHA-gate in kb-change-detect.py reads this field. Without this stamp,
    the gate never sees the new SHA → every repo is re-queued every run (C4).
    Entries not yet in the manifest are left unchanged (auto-registered on next
    reconciler run or by kb-remediate).
    """
    manifest_path = vault / "00-meta" / "kb-manifest.json"
    if not manifest_path.exists():
        return
    manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    updated = False
    for entry in manifest:
        if entry.get("name") == name:
            entry["last_documented_sha"] = head_sha
            updated = True
            break
    if not updated:
        return  # repo not yet registered; leave manifest intact
    _atomic_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Ollama discovery + startup (WSL2 ↔ Windows host)
# ---------------------------------------------------------------------------

def _ollama_win_exe_paths() -> list[Path]:
    """Return candidate Windows Ollama exe paths, user-profile-aware."""
    import os as _os
    candidates: list[Path] = []
    # KB_OLLAMA_EXE env var override
    env_override = _os.environ.get("KB_OLLAMA_EXE")
    if env_override:
        candidates.append(Path(env_override))
    # Per-user install (try to resolve Windows username from env or /mnt/c/Users)
    win_user = _os.environ.get("WIN_USERNAME") or _os.environ.get("USERPROFILE", "").replace("C:\\Users\\", "").split("\\")[0]
    if not win_user:
        # Fallback: enumerate /mnt/c/Users/ if accessible
        users_dir = Path("/mnt/c/Users")
        if users_dir.exists():
            for d in sorted(users_dir.iterdir()):
                if d.is_dir() and d.name not in ("Public", "Default", "All Users", "Default User"):
                    candidates.append(d / "AppData/Local/Programs/Ollama/ollama.exe")
    else:
        candidates.append(Path(f"/mnt/c/Users/{win_user}/AppData/Local/Programs/Ollama/ollama.exe"))
    # System-wide install
    candidates.append(Path("/mnt/c/Program Files/Ollama/ollama.exe"))
    return candidates


def _wsl_host_ip() -> str | None:
    """Return Windows host IP via the WSL2 default gateway (reliable; resolv.conf returns a virtual DNS)."""
    import subprocess as _sp
    try:
        out = _sp.check_output(["ip", "route"], text=True, timeout=3)
        for line in out.splitlines():
            if line.startswith("default"):
                parts = line.split()
                via_idx = parts.index("via") if "via" in parts else -1
                if via_idx >= 0 and via_idx + 1 < len(parts):
                    return parts[via_idx + 1]
    except Exception:
        pass
    return None


def _probe_ollama(url: str, timeout: float = 3.0) -> bool:
    """Return True if Ollama API responds at url."""
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _find_ollama_exe() -> Path | None:
    for p in _ollama_win_exe_paths():
        try:
            if p.exists():
                return p
        except PermissionError:
            continue
    return None


def _start_ollama_windows(exe: Path, port: int = 11434) -> bool:
    """Kill any running Ollama (may be 127.0.0.1-only) then restart with 0.0.0.0 binding.

    Uses PowerShell for reliable Windows process control from WSL2.
    Returns True if launch was attempted (caller must probe until ready).
    """
    import subprocess as _sp
    import time as _t
    ps = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    win_exe = str(exe).replace("/mnt/c/", "C:\\").replace("/", "\\")
    if not Path(ps).exists():
        return False
    try:
        _sp.run(
            [ps, "-NoProfile", "-Command",
             "Stop-Process -Name ollama -Force -ErrorAction SilentlyContinue"],
            check=False, capture_output=True, timeout=5,
        )
        _t.sleep(2)
        _sp.Popen(
            [ps, "-NoProfile", "-Command",
             f"$env:OLLAMA_HOST = '0.0.0.0:{port}'; "
             f"Start-Process -FilePath '{win_exe}' -ArgumentList 'serve' -WindowStyle Hidden"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True,
        )
        return True
    except Exception:
        return False


def resolve_ollama_url(configured_url: str) -> str:
    """Find a reachable Ollama endpoint, starting it if necessary.

    Priority:
      1. configured_url (KB_OLLAMA_URL / --ollama-url)
      2. Windows host IP:11434 (WSL2 — only if Ollama is bound to 0.0.0.0)
      3. Start Ollama via Windows exe with OLLAMA_HOST=0.0.0.0:11434, then re-probe

    Returns a reachable URL, or the original configured_url if all probes fail
    (caller will get a clean ConnectError and fall back to Claude API).
    """
    import time as _time
    from urllib.parse import urlparse as _urlparse
    port = _urlparse(configured_url).port or 11434

    # 1. Fast path — configured URL already works
    if _probe_ollama(configured_url):
        return configured_url

    # 2. WSL2: try Windows host IP (works when Ollama binds to 0.0.0.0)
    win_ip = _wsl_host_ip()
    win_url = f"http://{win_ip}:{port}" if win_ip else None
    if win_url and _probe_ollama(win_url):
        print(f"[ollama] reachable at Windows host {win_url}", file=sys.stderr)
        return win_url

    # 3. Try to start via Windows exe
    exe = _find_ollama_exe()
    if exe:
        print(f"[ollama] not reachable — starting via {exe} with OLLAMA_HOST=0.0.0.0:{port}",
              file=sys.stderr)
        launched = _start_ollama_windows(exe, port=port)
        if launched:
            for attempt in range(12):  # up to 12s
                _time.sleep(1)
                # Check win_url first — after restart with 0.0.0.0, gateway IP succeeds first
                if win_url and _probe_ollama(win_url):
                    print(f"[ollama] ready at {win_url}", file=sys.stderr)
                    return win_url
                if _probe_ollama(configured_url):
                    print(f"[ollama] ready at {configured_url}", file=sys.stderr)
                    return configured_url
            print("[ollama] timed out waiting for startup — will fall back to Claude API",
                  file=sys.stderr)
    else:
        print("[ollama] exe not found and not reachable — will fall back to Claude API",
              file=sys.stderr)

    return configured_url  # caller gets ConnectError → Claude API fallback


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------

def _call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout_secs: int,
) -> dict[str, Any]:
    """POST to Ollama /api/chat and return parsed prose dict.

    Raises httpx.ConnectError / httpx.ConnectTimeout when Ollama is unreachable.
    Raises ValueError when the response body cannot be parsed as JSON prose.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    resp = httpx.post(f"{ollama_url}/api/chat", json=payload, timeout=timeout_secs)
    resp.raise_for_status()
    content: str = resp.json()["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):  # I2: guard against list/string/etc. responses
        raise ValueError(f"expected JSON object from Ollama, got {type(parsed).__name__}")
    return parsed  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically using temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the zero-token KB sync pipeline. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Zero-token KB sync: Ollama prose pass + vault card write"
    )
    parser.add_argument("--repos-json", metavar="PATH",
                        help="JSON file with list of repo descriptors")
    parser.add_argument("--repos-data", metavar="JSON",
                        help="Inline JSON string of repos list (alternative to --repos-json)")
    parser.add_argument("--vault", required=True, metavar="PATH",
                        help="Absolute vault root path")
    parser.add_argument("--ollama-url", default="http://localhost:11434", metavar="URL",
                        help="Ollama base URL (default: http://localhost:11434)")
    parser.add_argument("--model", default="mistral:7b", metavar="NAME",
                        help="Ollama model name (default: mistral:7b)")
    parser.add_argument("--timeout-secs", type=int, default=120, metavar="INT",
                        help="Ollama request timeout in seconds (default: 120)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing anything")
    parser.add_argument("--stamp-manifest", action="store_true",
                        help="Stamp last_documented_sha in kb-manifest.json for repos in --repos-data, "
                             "then regenerate INDEX.agent.md. Used by the workflow fallback path.")
    args = parser.parse_args(argv)

    vault = Path(args.vault)

    # Load repos list — accept inline JSON (--repos-data) or a file path (--repos-json)
    try:
        if args.repos_data:
            repos: list[dict[str, Any]] = json.loads(args.repos_data)
        elif args.repos_json:
            repos = json.loads(Path(args.repos_json).read_text(encoding="utf-8"))
        else:
            print("error: one of --repos-json or --repos-data required", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"ERROR: failed to load repos list: {e}", file=sys.stderr)
        return 1
    if not isinstance(repos, list):
        print(f"ERROR: repos list must be a JSON array, got {type(repos).__name__}", file=sys.stderr)
        return 1

    # --stamp-manifest: stamp SHA for each repo in repos-data, then regenerate index.
    # Used by the workflow fallback path (Claude API pipeline) which writes cards via
    # synth agents but cannot stamp the manifest inside the workflow sandbox.
    if args.stamp_manifest:
        stamped: list[str] = []
        for repo in repos:
            name = repo.get("name", "")
            head_sha = repo.get("head_sha", "")
            if name and head_sha:
                try:
                    _stamp_manifest_sha(vault, name, head_sha)
                    stamped.append(name)
                    print(f"[stamp] {name}: {head_sha[:8]}", file=sys.stderr)
                except Exception as exc:
                    print(f"[warn] {name}: manifest stamp failed: {exc}", file=sys.stderr)
        index_stats: dict[str, Any] = {}
        try:
            index_stats = generate_index(vault)
        except Exception as exc:
            print(f"[warn] generate_index failed: {exc}", file=sys.stderr)
        print(json.dumps({"stamped": stamped, "index": index_stats}))
        return 0

    # Load scout-prompt.md template from same directory as this script
    prompt_template_path = _SKILL_DIR / "scout-prompt.md"
    try:
        prompt_template = prompt_template_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"ERROR: cannot read scout-prompt.md at {prompt_template_path}: {e}", file=sys.stderr)
        return 1

    documented: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # SHA-skip pass (defense-in-depth — orchestrator is authoritative)
    work_repos: list[dict[str, Any]] = []
    for repo in repos:
        head_sha = repo.get("head_sha", "")
        last_sha = repo.get("last_documented_sha", "")
        name = repo.get("name", "<unknown>")
        if head_sha and last_sha and head_sha == last_sha:
            print(f"[sha-skip] {name}: unchanged (head_sha == last_documented_sha)", file=sys.stderr)
            skipped.append({"name": name, "reason": "sha-match"})
        else:
            work_repos.append(repo)

    if args.dry_run:
        print(f"[dry-run] {len(work_repos)} repos to process, {len(skipped)} sha-skipped")
        for repo in work_repos:
            print(f"  would process: {repo.get('name')} ({repo.get('group')})")
        print(json.dumps({"documented": [], "failed": [], "skipped": skipped}, indent=2))
        return 0

    if not work_repos:
        print(json.dumps({"documented": documented, "failed": failed, "skipped": skipped}, indent=2))
        return 0

    # Resolve Ollama URL — probe configured address, Windows host IP, and attempt
    # auto-start via the Windows exe if not reachable (WSL2 ↔ Windows Ollama).
    ollama_url = resolve_ollama_url(args.ollama_url)

    # Sentinel — written BEFORE first card write; heartbeated after each write;
    # removed in finally so both clean and error exits clear it.
    sentinel_path = vault / "00-meta" / ".kb-sync-active"
    _write_sentinel(sentinel_path)

    try:
        for repo in work_repos:
            name = repo.get("name", "")
            group = repo.get("group", "")

            # Step a — read harvest cache
            cache_path = vault / "00-meta" / "scout-cache" / f"{name}.json"
            if not cache_path.exists():
                print(
                    f"[warn] {name}: harvest cache missing at {cache_path}, skipping",
                    file=sys.stderr,
                )
                failed.append({"name": name, "reason": "missing harvest cache"})
                continue

            try:
                structured: dict[str, Any] = json.loads(
                    cache_path.read_text(encoding="utf-8")
                )
            except Exception as e:
                print(f"[warn] {name}: harvest cache parse error: {e}", file=sys.stderr)
                failed.append({"name": name, "reason": f"harvest cache parse error: {e}"})
                continue

            # Step b — build scout prompt from template
            changed_files: list[str] = repo.get("changed_files") or []
            if changed_files:
                changed_hint = (
                    f"CHANGED FILES SINCE LAST DOC (focus prose on these): "
                    f"{', '.join(changed_files)}"
                )
            else:
                changed_hint = "Full prose pass (no prior baseline)."

            prompt = (
                prompt_template
                .replace("{REPO_PATH}", repo.get("path", ""))
                .replace("{REPO_NAME}", name)
                .replace("{GROUP}", group)
                .replace("{CHANGED_FILES_HINT}", changed_hint)
            )

            # C1: Inject harvest cache so Ollama can ground its prose in real data.
            # Ollama has no tool-use; the cache must be in the prompt text itself.
            cache_context = json.dumps(structured, indent=2, ensure_ascii=False)
            prompt += (
                "\n\nHARVEST CACHE (authoritative structured data — ground your prose"
                " in these facts):\n```json\n"
                + cache_context
                + "\n```\n\n"
                "The cache contains: identity (repo_url, branch, primary_binary,"
                " language), head_sha, cli (commands with description/flags), errors"
                " (with description/causes), adrs (architectural decisions),"
                " docs_present (list of docs/kb/ filenames), harvest_counts, and"
                " lineage keys (advances, phase, milestones).\n\n"
                "CRITICAL grounding rules:\n"
                "- next_command: emit ONLY if a concrete next step appears in the"
                " cli[], active/plan/ or .planning/ROADMAP section of the cache, or"
                " is explicitly stated in readme headings. NEVER fabricate a plausible"
                " command. If absent: null.\n"
                "- blockers: derive ONLY from actual blocking items evident in the"
                " cache (errors[], adrs with blocked status, or clear planning gaps)."
                " Do NOT invent blockers.\n"
                "- summary: ground every claim in the cache. Do not add claims not"
                " supported by the data.\n"
            )

            # Step c-d — call Ollama with one automatic retry on validation failure
            prose: dict[str, Any] | None = None
            for attempt in range(2):
                try:
                    raw = _call_ollama(
                        ollama_url, args.model, prompt, args.timeout_secs
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    print("KB_OLLAMA_UNAVAILABLE", file=sys.stderr)
                    return 2
                except Exception as e:
                    print(
                        f"[warn] {name}: Ollama error on attempt {attempt + 1}: {e}",
                        file=sys.stderr,
                    )
                    continue

                ok, reason = validate_scout(raw)
                if ok:
                    prose = raw
                    break
                print(
                    f"[warn] {name}: scout validation failed (attempt {attempt + 1}): {reason}",
                    file=sys.stderr,
                )

            if prose is None:
                failed.append({"name": name, "reason": "scout validation failed after retry"})
                continue

            # Step e — merge: structured keys are authoritative; prose augments.
            # Whitelist prose to only known prose keys so prose cannot overwrite
            # structured identity, head_sha, docs_present, or other harvest keys. (I3)
            prose_clean: dict[str, Any] = {k: v for k, v in prose.items() if k in PROSE_KEYS}
            merged: dict[str, Any] = {**structured, **prose_clean}

            # Normalize blocker severities so Ollama synonyms ("critical", "medium",
            # etc.) map to canonical enum values (low|med|high|crit). Runs BEFORE
            # card write so all downstream consumers see canonical values.
            for b in merged.get('blockers', []):
                if isinstance(b, dict):
                    b['severity'] = _norm_severity(b.get('severity', 'low'))

            # Step f — read existing operator fields from current card (if any)
            card_path = vault / "02-projects" / group / f"{name}.md"
            existing = read_existing_operator_fields(card_path)

            # Step g — render card
            card_text = render_card(merged, vault, repo, existing)

            # Step g (write) — atomic write to vault card path
            _atomic_write(card_path, card_text)

            # Step g-tasknotes — generate/update TaskNote files for this project's blockers.
            # rag_flag comes from `existing` (operator-owned field) NOT from `merged`
            # (merged = scout-cache structured ∪ prose, which has no rag_flag key).
            tn_stats = write_task_notes(
                project_slug=name,
                card_data={
                    **merged,
                    "group": group,
                    "rag_flag": existing.get("rag_flag") or "red",
                },
                vault_root=vault,
                dry_run=args.dry_run,
            )
            if any(tn_stats.values()):
                print(
                    f"[tasknotes] {name}: "
                    f"+{tn_stats['created']} created, "
                    f"~{tn_stats['updated']} updated, "
                    f"done={tn_stats['completed']}, "
                    f"archived={tn_stats['archived']}",
                    file=sys.stderr,
                )

            # Step h — atomic write merged cache back to scout-cache
            _atomic_write(cache_path, json.dumps(merged, indent=2, ensure_ascii=False))

            # Step i — heartbeat sentinel after each card write
            _write_sentinel(sentinel_path)

            head_sha = repo.get("head_sha", "")
            documented.append({
                "name": name,
                "group": group,
                "head_sha": head_sha,
                "next_command": merged.get("next_command"),
            })

            # Step i2 — stamp manifest last_documented_sha so SHA-gate closes on next run.
            # Non-fatal: a manifest write failure must not abort the rest of the sync.
            if head_sha:
                try:
                    _stamp_manifest_sha(vault, name, head_sha)
                except Exception as exc:
                    print(f"[warn] {name}: manifest sha stamp failed (non-fatal): {exc}", file=sys.stderr)

        # Step j — generate hot-index after all cards are written
        try:
            generate_index(vault)
        except Exception as exc:
            print(f"[warn] generate_index failed (non-fatal): {exc}", file=sys.stderr)

    finally:
        # Remove sentinel — fires on both normal and error exits
        sentinel_path.unlink(missing_ok=True)

    report = {"documented": documented, "failed": failed, "skipped": skipped}
    print(json.dumps(report, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Hot-index generation
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FLAG_ORDER: dict[str, int] = {"red": 0, "yellow": 1, "green": 2}
_VALID_FLAGS: set[str] = {"red", "yellow", "green"}
_MAX_INDEX_LINES = 190


def generate_index(vault: Path) -> dict[str, int]:
    """Scan 02-projects/ cards, render INDEX.agent.md hot-index.

    Reads all ``02-projects/**/*.md`` vault cards, filters to active project
    cards, and writes an agent-readable Markdown index to ``vault/INDEX.agent.md``
    (atomic write).  Also writes ``vault/00-meta/index-meta.json`` with counts.

    Returns {"count": N, "red": N, "yellow": N, "green": N}.
    """
    projects_dir = vault / "02-projects"
    cards: list[dict[str, str]] = []

    for md_file in sorted(projects_dir.glob("**/*.md")):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        m = _FM_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        if fm.get("type") != "project":
            continue
        if fm.get("status") == "archived":
            continue

        # stem (filename without .md) is the canonical manifest name.
        # title is display-only and may differ from the manifest `name` key.
        stem: str = md_file.stem
        title: str = str(fm.get("title") or stem)
        group: str = str(fm.get("group") or md_file.parent.name)
        classifier: str = str(fm.get("classifier") or "")
        raw_flag = str(fm.get("rag-flag") or "green").lower()
        rag_flag = raw_flag if raw_flag in _VALID_FLAGS else "green"
        next_command: str = str(fm.get("next-command") or "")

        # Summary: objective > problem > nextsteps[0] first sentence > ""
        summary = ""
        raw_obj = fm.get("objective")
        raw_prob = fm.get("problem")
        raw_ns = fm.get("nextsteps")
        if raw_obj:
            summary = str(raw_obj).split(".")[0].strip()
        elif raw_prob:
            summary = str(raw_prob).split(".")[0].strip()
        elif isinstance(raw_ns, list) and raw_ns:
            summary = str(raw_ns[0]).split(".")[0].strip()

        rel_path = str(md_file.relative_to(vault)).replace("\\", "/")

        last_sha: str = str(fm.get("last-documented-sha") or "")
        blocker_sev: str = str(fm.get("blocker-severity") or "")

        cards.append({
            "name": stem,        # canonical id (matches manifest `name` key)
            "title": title,      # display name (may differ from stem)
            "group": group,
            "classifier": classifier,
            "rag_flag": rag_flag,
            "next_command": next_command,
            "summary": summary,
            "rel_path": rel_path,
            "last_sha": last_sha,
            "blocker_severity": blocker_sev,
        })

    cards.sort(key=lambda c: (_FLAG_ORDER.get(c["rag_flag"], 2), c["group"], c["title"]))

    red_cards   = [c for c in cards if c["rag_flag"] == "red"]
    yellow_cards = [c for c in cards if c["rag_flag"] == "yellow"]
    green_cards  = [c for c in cards if c["rag_flag"] == "green"]

    count   = len(cards)
    red_n   = len(red_cards)
    yellow_n = len(yellow_cards)
    green_n  = len(green_cards)

    def _bullet(card: dict[str, str]) -> str:
        line = f"- **[{card['title']}]({card['rel_path']})** `{card['group']}`"
        if card["summary"]:
            line += f" — {card['summary']}"
        if card["next_command"]:
            line += f" Next: `{card['next_command']}`"
        return line

    lines: list[str] = [
        "# KB Project Index",
        "",
        f"_Auto-generated by kb-sync. {count} projects: {red_n} red · {yellow_n} yellow · {green_n} green._",
        "_Read individual cards only when you need full detail. Cards are in `02-projects/<group>/`._",
        "",
    ]

    if red_cards:
        lines += [f"## 🔴 Needs attention ({red_n})", ""]
        lines += [_bullet(c) for c in red_cards]
        lines.append("")

    if yellow_cards:
        lines += [f"## 🟡 In progress ({yellow_n})", ""]
        lines += [_bullet(c) for c in yellow_cards]
        lines.append("")

    lines += [f"## 🟢 Stable ({green_n})", ""]

    # Line-budget check — computed after red/yellow sections are committed
    remaining = _MAX_INDEX_LINES - len(lines) - 1   # reserve 1 for trailing ""
    if remaining >= len(green_cards):
        lines += [_bullet(c) for c in green_cards]
    else:
        slots = max(0, remaining - 1)
        lines += [_bullet(c) for c in green_cards[:slots]]
        omitted = len(green_cards) - slots
        lines.append(
            f"_...and {omitted} more stable projects — run `kb-context-pack.py discover` for the full list._"
        )

    lines.append("")

    content = "\n".join(lines)
    _atomic_write(vault / "INDEX.agent.md", content)

    meta: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": count,
        "red": red_n,
        "yellow": yellow_n,
        "green": green_n,
    }
    _atomic_write(vault / "00-meta" / "index-meta.json", json.dumps(meta, indent=2))

    # Reconcile manifest: sync last_documented_sha, blocker_severity, and rag from
    # vault cards; also auto-register vault cards missing from the manifest.
    # The stamp in the main sync path can silently fail (non-fatal catch);
    # this pass makes the manifest self-healing on every index rebuild.
    manifest_path = vault / "00-meta" / "kb-manifest.json"
    if manifest_path.exists():
        try:
            # Key by (name, group): stem is canonical (matches manifest `name`);
            # group disambiguates same-named projects across concept groups.
            # Keying by title alone caused two bugs: (1) title/name mismatch broke
            # reconciliation silently; (2) same-titled cards from different groups
            # overwrote each other in the dict (both adversarially confirmed).
            card_index: dict[tuple[str, str], dict[str, str]] = {
                (c["name"], c["group"]): {
                    "last_sha": c["last_sha"],
                    "blocker_severity": c["blocker_severity"],
                    "rag_flag": c["rag_flag"],
                    "classifier": c["classifier"],
                    "rel_path": c["rel_path"],
                }
                for c in cards
            }
            manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
            registered: set[tuple[str, str]] = {
                (entry.get("name", ""), entry.get("group", "")) for entry in manifest
            }
            changed = False
            for entry in manifest:
                key = (entry.get("name", ""), entry.get("group", ""))
                ci = card_index.get(key)
                if ci is None:
                    continue
                if ci["last_sha"] and entry.get("last_documented_sha") != ci["last_sha"]:
                    entry["last_documented_sha"] = ci["last_sha"]
                    changed = True
                if ci["blocker_severity"] != entry.get("blocker_severity", ""):
                    entry["blocker_severity"] = ci["blocker_severity"]
                    changed = True
                if ci["rag_flag"] and entry.get("rag") != ci["rag_flag"]:
                    entry["rag"] = ci["rag_flag"]
                    changed = True
            # Auto-register vault cards that have no manifest entry.
            # A project is discoverable by kb-change-detect only if it appears
            # in the manifest; cards written by the synth but never registered
            # become permanent orphans invisible to future syncs.
            for (card_name, card_group), ci in card_index.items():
                if (card_name, card_group) in registered:
                    continue
                manifest.append({
                    "name": card_name,
                    "group": card_group,
                    "classifier": ci["classifier"],
                    "rag": ci["rag_flag"],
                    "blocker_severity": ci["blocker_severity"],
                    "last_documented_sha": ci["last_sha"],
                    "card_path": ci["rel_path"],
                })
                print(f"[manifest] auto-registered orphan: {card_name}", file=sys.stderr)
                changed = True
            if changed:
                _atomic_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))
        except Exception as exc:
            print(f"[warn] manifest reconciliation failed (non-fatal): {exc}", file=sys.stderr)

    return {"count": count, "red": red_n, "yellow": yellow_n, "green": green_n}


if __name__ == "__main__":
    sys.exit(main())
