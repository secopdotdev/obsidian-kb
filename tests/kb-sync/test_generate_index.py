"""Tests for generate_index() in kb-sync-run.py.

All I/O uses tmp_path — no real vault reads.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loader (hyphenated filename cannot be bare-imported)
# ---------------------------------------------------------------------------

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
_RUN_SCRIPT = _SKILL / "kb-sync-run.py"


def _load_run() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kb_sync_run", str(_RUN_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(
    vault: Path,
    name: str,
    group: str,
    *,
    rag_flag: str = "green",
    status: str = "active",
    card_type: str = "project",
    next_command: str = "",
    title: str = "",
    objective: str = "",
    last_sha: str = "",
    blocker_severity: str = "",
) -> Path:
    """Write a minimal project card with YAML frontmatter into tmp vault."""
    card_dir = vault / "02-projects" / group
    card_dir.mkdir(parents=True, exist_ok=True)
    resolved_title = title or name
    frontmatter_lines = [
        "---",
        f"type: {card_type}",
        f'title: "{resolved_title}"',
        f"group: {group}",
        f"rag-flag: {rag_flag}",
        f"status: {status}",
        f'next-command: "{next_command}"',
    ]
    if objective:
        frontmatter_lines.append(f'objective: "{objective}"')
    if last_sha:
        frontmatter_lines.append(f'last-documented-sha: "{last_sha}"')
    if blocker_severity:
        frontmatter_lines.append(f"blocker-severity: {blocker_severity}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(f"# {resolved_title}")
    frontmatter_lines.append("")
    card_path = card_dir / f"{name}.md"
    card_path.write_text("\n".join(frontmatter_lines), encoding="utf-8")
    return card_path


def _meta_dir(vault: Path) -> Path:
    d = vault / "00-meta"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_index_sorted_red_yellow_green(tmp_path: Path) -> None:
    """Cards must appear in red → yellow → green order in INDEX.agent.md."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "green-proj", "group-a", rag_flag="green")
    _make_card(tmp_path, "red-proj", "group-a", rag_flag="red")
    _make_card(tmp_path, "yellow-proj", "group-a", rag_flag="yellow")

    mod = _load_run()
    result = mod.generate_index(tmp_path)

    assert result["count"] == 3
    assert result["red"] == 1
    assert result["yellow"] == 1
    assert result["green"] == 1

    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    red_pos = content.find("red-proj")
    yellow_pos = content.find("yellow-proj")
    green_pos = content.find("green-proj")
    assert red_pos != -1
    assert yellow_pos != -1
    assert green_pos != -1
    assert red_pos < yellow_pos < green_pos, (
        f"Expected red ({red_pos}) < yellow ({yellow_pos}) < green ({green_pos})"
    )


def test_index_truncates_at_190_lines(tmp_path: Path) -> None:
    """When green section would push total > 190 lines, truncation message appears."""
    _meta_dir(tmp_path)
    # With no red/yellow, header uses 7 lines before green bullets.
    # remaining = 190 - 7 = 183; need > 183 green cards to trigger truncation.
    for i in range(185):
        _make_card(tmp_path, f"proj-{i:03d}", "group-a", rag_flag="green")

    mod = _load_run()
    mod.generate_index(tmp_path)

    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) <= 195, f"Expected ≤195 lines, got {len(lines)}"
    assert any("more stable projects" in ln for ln in lines), (
        "Expected truncation message in output"
    )


def test_index_atomic_write(tmp_path: Path) -> None:
    """INDEX.agent.md must be written atomically — final file present, no .tmp left."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "alpha", "group-a", rag_flag="green")

    mod = _load_run()
    mod.generate_index(tmp_path)

    index_path = tmp_path / "INDEX.agent.md"
    assert index_path.exists(), "INDEX.agent.md was not created"

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert not tmp_files, f"Stale .tmp files found in vault root: {tmp_files}"


def test_index_skips_non_project_types(tmp_path: Path) -> None:
    """Cards with type != 'project' (adr, runbook) must be excluded."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "my-adr", "group-a", card_type="adr")
    _make_card(tmp_path, "my-runbook", "group-a", card_type="runbook")
    _make_card(tmp_path, "real-proj", "group-a", card_type="project")

    mod = _load_run()
    result = mod.generate_index(tmp_path)

    assert result["count"] == 1, f"Expected 1 project card, got {result['count']}"
    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    assert "real-proj" in content
    assert "my-adr" not in content
    assert "my-runbook" not in content


def test_index_skips_archived_status(tmp_path: Path) -> None:
    """Cards with status == 'archived' must be excluded."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "archived-proj", "group-a", status="archived")
    _make_card(tmp_path, "active-proj", "group-a", status="active")

    mod = _load_run()
    result = mod.generate_index(tmp_path)

    assert result["count"] == 1
    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    assert "active-proj" in content
    assert "archived-proj" not in content


def test_index_meta_json_written(tmp_path: Path) -> None:
    """00-meta/index-meta.json must be written with count/red/yellow/green keys."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "proj-r", "group-a", rag_flag="red")
    _make_card(tmp_path, "proj-y", "group-a", rag_flag="yellow")
    _make_card(tmp_path, "proj-g1", "group-a", rag_flag="green")
    _make_card(tmp_path, "proj-g2", "group-a", rag_flag="green")

    mod = _load_run()
    mod.generate_index(tmp_path)

    meta_path = tmp_path / "00-meta" / "index-meta.json"
    assert meta_path.exists(), "index-meta.json was not created"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["count"] == 4
    assert meta["red"] == 1
    assert meta["yellow"] == 1
    assert meta["green"] == 2
    assert "generated_at" in meta


def test_index_next_command_omitted_when_empty(tmp_path: Path) -> None:
    """Bullet for a card with empty next-command must not contain 'Next:'."""
    _meta_dir(tmp_path)
    _make_card(tmp_path, "no-cmd", "group-a", rag_flag="green", next_command="")

    mod = _load_run()
    mod.generate_index(tmp_path)

    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    bullet_line = next(ln for ln in content.splitlines() if "no-cmd" in ln)
    assert "Next:" not in bullet_line, f"Expected no 'Next:' in: {bullet_line!r}"


def test_index_next_command_included(tmp_path: Path) -> None:
    """Bullet for a card with a next-command must include 'Next: `<cmd>`'."""
    _meta_dir(tmp_path)
    _make_card(
        tmp_path,
        "has-cmd",
        "group-a",
        rag_flag="green",
        next_command="python3 tools/run.py --start",
    )

    mod = _load_run()
    mod.generate_index(tmp_path)

    content = (tmp_path / "INDEX.agent.md").read_text(encoding="utf-8")
    bullet_line = next(ln for ln in content.splitlines() if "has-cmd" in ln)
    assert "Next: `python3 tools/run.py --start`" in bullet_line, (
        f"Expected next-command in: {bullet_line!r}"
    )


# ---------------------------------------------------------------------------
# Manifest reconciliation tests
# ---------------------------------------------------------------------------

def _make_manifest(meta_dir: Path, entries: list[dict]) -> Path:
    """Write kb-manifest.json with given entries."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = meta_dir / "kb-manifest.json"
    manifest_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return manifest_path


def test_generate_index_reconciles_stale_manifest_sha(tmp_path: Path) -> None:
    """generate_index() must update manifest last_documented_sha from vault card."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "myrepo", "group-a", rag_flag="green", last_sha="abc1234")
    _make_manifest(meta, [{"name": "myrepo", "group": "group-a",
                           "last_documented_sha": "old000", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in updated if e["name"] == "myrepo")
    assert entry["last_documented_sha"] == "abc1234", "Stale SHA must be updated from card"


def test_generate_index_reconciles_blocker_severity(tmp_path: Path) -> None:
    """generate_index() must update manifest blocker_severity from vault card."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "myrepo", "group-a", rag_flag="red", blocker_severity="high")
    _make_manifest(meta, [{"name": "myrepo", "group": "group-a",
                           "last_documented_sha": "", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in updated if e["name"] == "myrepo")
    assert entry["blocker_severity"] == "high", "blocker_severity must be synced from card"


def test_generate_index_manifest_reconcile_no_change_when_matching(tmp_path: Path) -> None:
    """generate_index() must not re-write manifest when sha, blocker_severity, and rag all match."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "stable", "group-a", rag_flag="green", last_sha="abc123")
    mp = _make_manifest(meta, [{"name": "stable", "group": "group-a",
                                "last_documented_sha": "abc123",
                                "blocker_severity": "", "rag": "green"}])
    mtime_before = mp.stat().st_mtime

    _load_run().generate_index(tmp_path)

    mtime_after = mp.stat().st_mtime
    assert mtime_after == mtime_before, "Manifest must not be rewritten when already up to date"


def test_generate_index_auto_registers_orphan_cards(tmp_path: Path) -> None:
    """generate_index() must auto-register vault cards absent from the manifest."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "unregistered", "group-a", rag_flag="green", last_sha="abc123")
    _make_manifest(meta, [{"name": "other", "group": "group-b",
                           "last_documented_sha": "xyz", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    manifest = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in manifest}
    assert "other" in names, "Pre-existing entry must be preserved"
    assert "unregistered" in names, "Orphan card must be auto-registered"
    new_entry = next(e for e in manifest if e["name"] == "unregistered")
    assert new_entry["rag"] == "green"
    assert new_entry["last_documented_sha"] == "abc123"


def test_generate_index_manifest_reconcile_updates_both_fields(tmp_path: Path) -> None:
    """generate_index() must update both SHA and blocker_severity in one pass."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "proj", "group-a", rag_flag="yellow",
               last_sha="newsha1", blocker_severity="med")
    _make_manifest(meta, [{"name": "proj", "group": "group-a",
                           "last_documented_sha": "oldsha0", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in updated if e["name"] == "proj")
    assert entry["last_documented_sha"] == "newsha1"
    assert entry["blocker_severity"] == "med"


def test_generate_index_reconciles_rag_field(tmp_path: Path) -> None:
    """generate_index() must sync manifest 'rag' from vault card 'rag-flag'."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "proj", "group-a", rag_flag="red")
    _make_manifest(meta, [{"name": "proj", "group": "group-a",
                           "rag": "yellow", "last_documented_sha": "", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in updated if e["name"] == "proj")
    assert entry["rag"] == "red", "manifest rag must be updated from vault card rag-flag"


def test_generate_index_rag_no_change_when_matching(tmp_path: Path) -> None:
    """generate_index() must not rewrite manifest when rag already matches card."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "proj", "group-a", rag_flag="green")
    mp = _make_manifest(meta, [{"name": "proj", "group": "group-a",
                                "rag": "green", "last_documented_sha": "", "blocker_severity": ""}])
    mtime_before = mp.stat().st_mtime

    _load_run().generate_index(tmp_path)

    mtime_after = mp.stat().st_mtime
    assert mtime_after == mtime_before, "Manifest must not be rewritten when rag already matches"


def test_generate_index_reconciles_all_three_fields(tmp_path: Path) -> None:
    """generate_index() must update sha, blocker_severity, and rag together."""
    meta = _meta_dir(tmp_path)
    _make_card(tmp_path, "proj", "group-a", rag_flag="red",
               last_sha="abc999", blocker_severity="high")
    _make_manifest(meta, [{"name": "proj", "group": "group-a", "rag": "green",
                           "last_documented_sha": "old000", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in updated if e["name"] == "proj")
    assert entry["last_documented_sha"] == "abc999"
    assert entry["blocker_severity"] == "high"
    assert entry["rag"] == "red"


# ---------------------------------------------------------------------------
# Manifest orphan auto-registration tests (Fix C)
# ---------------------------------------------------------------------------

def _make_card_with_classifier(
    vault: Path,
    name: str,
    group: str,
    classifier: str = "Tool Bay",
    *,
    rag_flag: str = "green",
    last_sha: str = "",
    blocker_severity: str = "",
) -> Path:
    """Write a card with a classifier field for orphan-registration tests."""
    card_dir = vault / "02-projects" / group
    card_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f'title: "{name}"',
        "type: project",
        f"group: {group}",
        f'classifier: "{classifier}"',
        f"rag-flag: {rag_flag}",
        "status: active",
    ]
    if last_sha:
        lines.append(f'last-documented-sha: "{last_sha}"')
    if blocker_severity:
        lines.append(f"blocker-severity: {blocker_severity}")
    lines += ["---", "", f"# {name}", ""]
    card_path = card_dir / f"{name}.md"
    card_path.write_text("\n".join(lines), encoding="utf-8")
    return card_path


def test_generate_index_registers_orphan_card(tmp_path: Path) -> None:
    """A vault card absent from the manifest must be auto-registered."""
    meta = _meta_dir(tmp_path)
    _make_card_with_classifier(tmp_path, "new-proj", "group-a", "Launchpad",
                               rag_flag="yellow", last_sha="sha999")
    _make_manifest(meta, [{"name": "other", "group": "group-b", "rag": "green",
                           "last_documented_sha": "", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in updated}
    assert "new-proj" in names, "Orphan card must be auto-registered in manifest"
    entry = next(e for e in updated if e["name"] == "new-proj")
    assert entry["group"] == "group-a"
    assert entry["rag"] == "yellow"
    assert entry["last_documented_sha"] == "sha999"
    assert entry["classifier"] == "Launchpad"
    assert "card_path" in entry


def test_generate_index_does_not_duplicate_registered_entry(tmp_path: Path) -> None:
    """A card already in the manifest must not be duplicated on repeated runs."""
    meta = _meta_dir(tmp_path)
    _make_card_with_classifier(tmp_path, "proj", "group-a", rag_flag="green", last_sha="abc")
    _make_manifest(meta, [{"name": "proj", "group": "group-a", "rag": "green",
                           "last_documented_sha": "abc", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    proj_entries = [e for e in updated if e["name"] == "proj"]
    assert len(proj_entries) == 1, "Registered card must appear exactly once"


def test_generate_index_reconciles_when_title_differs_from_stem(tmp_path: Path) -> None:
    """Reconciliation must use filename stem (not title) to match manifest name."""
    meta = _meta_dir(tmp_path)
    # Card filename is my-proj.md (stem="my-proj") but title differs
    _make_card(tmp_path, "my-proj", "group-a", rag_flag="red", last_sha="new111",
               title="My Project (Display Name)")
    _make_manifest(meta, [{"name": "my-proj", "group": "group-a",
                           "last_documented_sha": "old000", "rag": "green", "blocker_severity": ""}])

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    proj_entries = [e for e in updated if e["name"] == "my-proj"]
    assert len(proj_entries) == 1, "Title/stem mismatch must not create phantom duplicate"
    assert proj_entries[0]["last_documented_sha"] == "new111", "SHA must be reconciled despite title diff"
    assert proj_entries[0]["rag"] == "red", "rag must be reconciled despite title diff"


def test_generate_index_registers_multiple_orphans(tmp_path: Path) -> None:
    """Multiple orphan cards must all be auto-registered in one pass."""
    meta = _meta_dir(tmp_path)
    _make_card_with_classifier(tmp_path, "alpha", "group-a", rag_flag="red")
    _make_card_with_classifier(tmp_path, "beta", "group-a", rag_flag="yellow")
    _make_manifest(meta, [])  # empty manifest

    _load_run().generate_index(tmp_path)

    updated = json.loads((meta / "kb-manifest.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in updated}
    assert "alpha" in names, "alpha must be registered"
    assert "beta" in names, "beta must be registered"
