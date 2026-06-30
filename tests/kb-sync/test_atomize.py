import json, subprocess, sys, shutil
from pathlib import Path
import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
ATOMIZE = SKILL / "kb-atomize.py"
FIX = Path(__file__).resolve().parent / "fixtures"

def run(cache, vault, *extra):
    return subprocess.run([sys.executable, str(ATOMIZE), "--cache", str(cache),
                           "--vault", str(vault), "--date", "2026-06-13", *extra],
                          capture_output=True, text=True)

@pytest.fixture
def vault(tmp_path):
    (tmp_path / "04-cli-errors").mkdir(); (tmp_path / "03-adr").mkdir()
    (tmp_path / "00-meta").mkdir()
    (tmp_path / "00-meta" / "retired-projects.txt").write_text("", encoding="utf-8")
    return tmp_path

@pytest.fixture
def cache(tmp_path):
    c = tmp_path / "cache"; c.mkdir()
    shutil.copy(FIX / "example-toolkit.json", c / "example-toolkit.json")
    return c

def test_projects_one_note_per_item(cache, vault):
    # ADR-0005: err/ADR are projected by DEFAULT (deterministic harvest = clean merge).
    r = run(cache, vault); assert r.returncode == 0, r.stderr
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()
    assert (vault / "04-cli-errors" / "err-example-toolkit-aadsts65002.md").exists()
    assert (vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md").exists()


def test_errors_adrs_projected_by_default(cache, vault):
    # ADR-0005: a normal run projects CLI + err + ADR + blockers (all layers on by default).
    r = run(cache, vault); assert r.returncode == 0, r.stderr
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()       # CLI projected
    assert (vault / "08-blockers" / "blk-example-toolkit-credential-purge-gap.md").exists()   # blockers projected
    assert (vault / "04-cli-errors" / "err-example-toolkit-aadsts65002.md").exists()          # err projected
    assert (vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md").exists()         # ADR projected


def test_frozen_errors_adrs_flag_skips_err_adr(cache, vault):
    # ADR-0005: --frozen-errors-adrs is the OPT-OUT; a frozen run skips err + ADR
    # but still projects CLI + blockers (mirrors the old frozen-by-default behaviour).
    r = run(cache, vault, "--frozen-errors-adrs"); assert r.returncode == 0, r.stderr
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()       # CLI projected
    assert (vault / "08-blockers" / "blk-example-toolkit-credential-purge-gap.md").exists()   # blockers projected
    assert not (vault / "04-cli-errors" / "err-example-toolkit-aadsts65002.md").exists()      # err frozen
    assert not (vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md").exists()     # ADR frozen


def test_stable_slug_reprojection_idempotent(cache, vault):
    # ADR-0005: deterministic slugs make a re-scrape a clean merge. Running atomize
    # twice over the same cache yields byte-identical err/ADR notes with no new
    # duplicates (same file names, mtime unchanged on second run).
    r = run(cache, vault); assert r.returncode == 0, r.stderr
    err_note = vault / "04-cli-errors" / "err-example-toolkit-aadsts65002.md"
    adr_note = vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md"
    assert err_note.exists() and adr_note.exists()
    err_text = err_note.read_text(encoding="utf-8")
    adr_text = adr_note.read_text(encoding="utf-8")
    err_mtime = err_note.stat().st_mtime_ns
    adr_mtime = adr_note.stat().st_mtime_ns
    # Snapshot directory listings before the second run.
    err_files_before = set(p.name for p in (vault / "04-cli-errors").glob("err-*.md"))
    adr_files_before = set(p.name for p in (vault / "03-adr").glob("*-adr-*.md"))
    # Second run — must be byte-identical (no new files, no re-writes).
    r2 = run(cache, vault); assert r2.returncode == 0, r2.stderr
    assert err_note.read_text(encoding="utf-8") == err_text         # byte-identical
    assert adr_note.read_text(encoding="utf-8") == adr_text         # byte-identical
    assert err_note.stat().st_mtime_ns == err_mtime                 # not re-written
    assert adr_note.stat().st_mtime_ns == adr_mtime                 # not re-written
    err_files_after = set(p.name for p in (vault / "04-cli-errors").glob("err-*.md"))
    adr_files_after = set(p.name for p in (vault / "03-adr").glob("*-adr-*.md"))
    assert err_files_after == err_files_before                      # no duplicates
    assert adr_files_after == adr_files_before                      # no duplicates


def test_cli_slug_deterministic_from_command(cache, vault):
    # ADR-0004: the CLI note slug is derived from `command` (basename, ext-stripped),
    # NOT from the scout's `slug` — stable across path/extension variation, so a
    # re-scrape merges onto the existing note instead of minting a drifted slug.
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    d["cli"] = [{"slug": "WRONG-scout-slug", "command": "incident-response/falcon-rtr-run.py",
                 "invocation": "python incident-response/falcon-rtr-run.py", "flags": []}]
    p.write_text(json.dumps(d), encoding="utf-8"); run(cache, vault)
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()
    assert not (vault / "04-cli-errors" / "cmd-example-toolkit-WRONG-scout-slug.md").exists()

def test_cmd_frontmatter(cache, vault):
    run(cache, vault)
    t = (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").read_text(encoding="utf-8")
    assert 'tool: "example-toolkit"' in t
    assert 'group/missionops' in t
    assert 'last-documented-sha: "60e4d14"' in t
    assert 'stale: false' in t

def test_idempotent(cache, vault):
    run(cache, vault)
    note = vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md"
    first = note.read_text(encoding="utf-8"); mtime = note.stat().st_mtime_ns
    run(cache, vault)
    assert note.read_text(encoding="utf-8") == first
    assert note.stat().st_mtime_ns == mtime


def _add_cli(cache):
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    d["cli"].append({"slug":"falcon-contain-device","command":"falcon-contain-device",
                     "invocation":"python incident-response/falcon-contain-device.py","flags":[]})
    p.write_text(json.dumps(d), encoding="utf-8")

def test_merge_adds_only_new(cache, vault):
    run(cache, vault)
    keep = vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md"
    b = keep.read_text(encoding="utf-8")
    _add_cli(cache); run(cache, vault)
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-contain-device.md").exists()
    assert keep.read_text(encoding="utf-8") == b

def test_absent_cli_slug_untouched_accumulate_only(cache, vault):
    # ADR-0004: CLI is accumulate-only — a command absent from a later (thin) scout
    # is LEFT UNTOUCHED (byte-identical), NEVER stale-flagged. An incomplete scout
    # must not flag a live command; removal happens only via retirement.
    _add_cli(cache); run(cache, vault)
    extra = vault / "04-cli-errors" / "cmd-example-toolkit-falcon-contain-device.md"
    before = extra.read_text(encoding="utf-8")
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    d["cli"] = [c for c in d["cli"] if c["slug"] != "falcon-contain-device"]
    p.write_text(json.dumps(d), encoding="utf-8"); run(cache, vault)
    assert extra.exists()
    assert extra.read_text(encoding="utf-8") == before    # untouched, not staled
    assert "stale: false" in before

def test_retirement_deletes(cache, vault):
    run(cache, vault)
    (vault / "00-meta" / "retired-projects.txt").write_text("example-toolkit\n", encoding="utf-8")
    run(cache, vault)
    assert not (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()

def test_unscouted_nonretired_untouched(cache, vault):
    foreign = vault / "04-cli-errors" / "cmd-other-tool-thing.md"
    foreign.write_text('---\ntype: cli\ntool: "other-tool"\nstale: false\n---\n# cmd-other-tool-thing\n', encoding="utf-8")
    b = foreign.read_text(encoding="utf-8")
    run(cache, vault)
    assert foreign.exists() and foreign.read_text(encoding="utf-8") == b


def test_index_regen(cache, vault):
    idx = vault / "04-cli-errors" / "_INDEX.md"
    idx.write_text("---\ntype: moc\n---\n# CLI & errors\n<!-- KB-SYNC:ROWS:START -->\nstale\n<!-- KB-SYNC:ROWS:END -->\n", encoding="utf-8")
    run(cache, vault)
    t = idx.read_text(encoding="utf-8")
    assert "[[cmd-example-toolkit-falcon-rtr-run]]" in t
    assert "# CLI & errors" in t          # shell preserved
    assert "stale\n" not in t              # old rows replaced
    assert "falcon-rtr-run.py" in t        # description is the invocation, not nav boilerplate

def test_index_idempotent(cache, vault):
    idx = vault / "04-cli-errors" / "_INDEX.md"
    idx.write_text("# CLI & errors\n<!-- KB-SYNC:ROWS:START -->\n<!-- KB-SYNC:ROWS:END -->\n", encoding="utf-8")
    run(cache, vault); first = idx.read_text(encoding="utf-8"); m = idx.stat().st_mtime_ns
    run(cache, vault)
    assert idx.read_text(encoding="utf-8") == first and idx.stat().st_mtime_ns == m


def _fm_block(text):
    """Return the YAML frontmatter (between the first and second `---`)."""
    parts = text.split("---", 2)
    assert len(parts) >= 3, "note has no `---`-fenced frontmatter block"
    return parts[1]

def test_yaml_escapes_quotes_in_frontmatter(cache, vault):
    # FIX 1 regression: a frontmatter-bound scalar carrying `"` and `\` (error
    # `code` renders into the YAML block as `code: "<value>"`) must stay valid
    # YAML. Pre-fix, _yaml_str wrapped without escaping -> the bare `"` closes
    # the quoted scalar and the block is unparseable.
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    d["errors"][0]["code"] = '400 "AADSTS65002" \\ barred'
    p.write_text(json.dumps(d), encoding="utf-8")
    r = run(cache, vault); assert r.returncode == 0, r.stderr
    note = vault / "04-cli-errors" / "err-example-toolkit-aadsts65002.md"
    t = note.read_text(encoding="utf-8")
    fm = _fm_block(t)
    try:
        import yaml
        parsed = yaml.safe_load(fm)            # must NOT raise on the escaped scalar
        assert parsed["code"] == '400 "AADSTS65002" \\ barred'
    except ImportError:
        # No PyYAML: assert the escaped sequence is present and no bare `"`
        # prematurely closes the quoted code scalar.
        assert r'\"AADSTS65002\"' in t
        assert 'code: "400 \\"AADSTS65002\\" \\\\ barred"' in t

def test_projects_blocker_note(cache, vault):
    (vault / "08-blockers").mkdir(exist_ok=True)
    run(cache, vault)
    f = vault / "08-blockers" / "blk-example-toolkit-credential-purge-gap.md"
    assert f.exists()
    t = f.read_text(encoding="utf-8")
    assert 'type: blocker' in t
    assert 'project: "example-toolkit"' in t
    assert 'severity: "med"' in t
    assert 'severity-rank: 2' in t
    assert 'stale: false' in t

def test_resolved_blocker_goes_stale(cache, vault):
    (vault / "08-blockers").mkdir(exist_ok=True)
    run(cache, vault)
    note = vault / "08-blockers" / "blk-example-toolkit-credential-purge-gap.md"
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    d["blockers"] = []   # blocker resolved
    p.write_text(json.dumps(d), encoding="utf-8"); run(cache, vault)
    assert note.exists()  # NOT deleted (history preserved)
    assert "stale: true" in note.read_text(encoding="utf-8")

def test_adr_options_table(cache, vault):
    # Task 10: an ADR whose scout carries options[] renders an Options-compared
    # table + a Recommendation line. Pipe chars inside a cell are escaped so the
    # markdown table stays well-formed.
    run(cache, vault)
    t = (vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md").read_text(encoding="utf-8")
    assert "## Options compared" in t
    assert "| Option | Pros | Cons | Cost |" in t
    assert "OpenBao" in t and "DPAPI" in t
    assert "**Recommendation:** OpenBao" in t
    assert r"self-host \| audited" in t   # literal pipe in a cell escaped

def test_adr_without_options_no_table(cache, vault):
    # Graceful degrade: an ADR with no options[] emits NO Options section.
    p = cache / "example-toolkit.json"; d = json.loads(p.read_text(encoding="utf-8"))
    for a in d["adrs"]:
        a.pop("options", None); a.pop("recommendation", None)
    p.write_text(json.dumps(d), encoding="utf-8"); run(cache, vault)
    t = (vault / "03-adr" / "example-toolkit-adr-0001-design-lessons.md").read_text(encoding="utf-8")
    assert "## Options compared" not in t
    assert "Recommendation:" not in t

def test_broken_cache_file_skipped_not_fatal(cache, vault):
    # FIX 2 regression: a valid-JSON cache missing a required key must skip only
    # that file (stderr `skip <file>`), NOT abort the whole run. `broken.json`
    # sorts before `example-toolkit.json`, so pre-fix the un-widened try lets the
    # KeyError propagate and the good owner's notes are never written.
    (cache / "broken.json").write_text(
        json.dumps({"group": "3.0-work", "head_sha": "deadbee", "cli": []}),
        encoding="utf-8")
    r = run(cache, vault)
    assert r.returncode == 0, r.stderr
    assert (vault / "04-cli-errors" / "cmd-example-toolkit-falcon-rtr-run.md").exists()
    assert "skip broken.json" in r.stderr
