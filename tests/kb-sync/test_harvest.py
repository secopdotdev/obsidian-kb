"""Tests for kb-harvest.py — deterministic per-repo structured extractor.

TDD: written before implementation to define the acceptance criteria for Task 3
(scaffold + non-CLI parsers: harvest_identity, harvest_docs_present, harvest_adrs,
harvest_structured).

Import pattern: uses importlib (hyphenated filename cannot be bare-imported).
"""
import importlib.util
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
HARVEST_SCRIPT = SKILL / "kb-harvest.py"
FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "repo"


def _load_harvest():
    """Load kb-harvest.py as a module via importlib (hyphen-safe)."""
    spec = importlib.util.spec_from_file_location("kb_harvest", str(HARVEST_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# harvest_adrs
# ---------------------------------------------------------------------------

def test_harvest_adrs_one_item(tmp_path):
    """fixture repo has one ADR → list of length 1."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    assert len(adrs) == 1


def test_harvest_adrs_id_from_filename(tmp_path):
    """id is extracted from the leading number in the filename (frontmatter has no adr-id)."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    assert adrs[0]["id"] == "0001"


def test_harvest_adrs_status_accepted(tmp_path):
    """status from frontmatter = 'accepted'."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    assert adrs[0]["status"] == "accepted"


def test_harvest_adrs_title_from_frontmatter(tmp_path):
    """title is pulled from the frontmatter `title` field."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    assert adrs[0]["title"] == "Secrets management approach"


def test_harvest_adrs_slug_deterministic():
    """slug is a kebab-case, period-free, collapse-trimmed derivation of the title."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    slug = adrs[0]["slug"]
    # Must be lowercase, no spaces, no non-alnum chars except hyphens
    assert slug == slug.lower()
    assert " " not in slug
    assert "." not in slug
    assert "_" not in slug
    assert slug == "secrets-management-approach"


def test_harvest_adrs_options_list(tmp_path):
    """options list has length 2 (two options in the '## Considered options' section)."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    options = adrs[0]["options"]
    assert options is not None
    assert len(options) == 2


def test_harvest_adrs_option_fields(tmp_path):
    """each option dict has name, pros, cons, cost keys."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    options = adrs[0]["options"]
    for opt in options:
        assert "name" in opt
        assert "pros" in opt
        assert "cons" in opt
        assert "cost" in opt


def test_harvest_adrs_option_names(tmp_path):
    """option names match what's in the fixture (OpenBao, DPAPI)."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    names = [o["name"] for o in adrs[0]["options"]]
    assert "OpenBao" in names
    assert "DPAPI" in names


def test_harvest_adrs_recommendation(tmp_path):
    """recommendation is parsed from 'Recommendation:' line."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    rec = adrs[0]["recommendation"]
    assert rec is not None
    assert "OpenBao" in rec


def test_harvest_adrs_missing_dir_returns_empty():
    """harvest_adrs on a repo with no ADR dirs returns []."""
    h = _load_harvest()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        adrs = h.harvest_adrs(Path(td))
    assert adrs == []


def test_harvest_adrs_malformed_file_skipped(tmp_path):
    """A malformed ADR file with no id signal is skipped; the valid file is still returned.

    The filter: a file with no leading-digit filename AND no title/status frontmatter
    is skipped (would be a README, _INDEX, or template — NEVER invented as an ADR).
    A file with a leading digit (0002-...) is parsed even if frontmatter-less.
    """
    import shutil
    # Copy fixture repo into tmp_path so we can add files safely
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    # A file with no leading digit AND no frontmatter → filtered out (not an ADR)
    bad = repo / "docs" / "adr" / "README.md"
    bad.write_text("NOT YAML\n\nno frontmatter fence, no leading digit\n", encoding="utf-8")
    h = _load_harvest()
    adrs = h.harvest_adrs(repo)
    # Valid ADR still present, README.md not included as a bogus ADR
    assert any(a["id"] == "0001" for a in adrs)
    ids = [a["id"] for a in adrs]
    assert "README" not in ids


def test_harvest_adrs_template_file_skipped(tmp_path):
    """A template/index file without a leading digit and without ADR frontmatter is skipped."""
    import shutil
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    # Simulate adr-tools 0000-template or MADR template (common in real repos)
    template = repo / "docs" / "adr" / "_INDEX.md"
    template.write_text("# Index\n\nAll ADRs listed here.\n", encoding="utf-8")
    h = _load_harvest()
    adrs = h.harvest_adrs(repo)
    ids = [a["id"] for a in adrs]
    assert "_INDEX" not in ids
    assert len(adrs) == 1  # only the real ADR


def test_harvest_adrs_sorted_by_id_slug():
    """Multiple ADRs are sorted (id, slug) for determinism."""
    h = _load_harvest()
    # Fixture only has one; just verify the list is consistently ordered with itself
    adrs = h.harvest_adrs(FIXTURE_REPO)
    pairs = [(a["id"], a["slug"]) for a in adrs]
    assert pairs == sorted(pairs)


def test_harvest_adrs_date_from_frontmatter():
    """date is extracted from frontmatter `date-decided` field."""
    h = _load_harvest()
    adrs = h.harvest_adrs(FIXTURE_REPO)
    assert adrs[0]["date"] == "2026-05-01"


# ---------------------------------------------------------------------------
# harvest_docs_present
# ---------------------------------------------------------------------------

def test_harvest_docs_present_returns_sorted_list():
    """docs/kb/ has one .md file → ['architecture.md']."""
    h = _load_harvest()
    docs = h.harvest_docs_present(FIXTURE_REPO)
    assert docs == ["architecture.md"]


def test_harvest_docs_present_sorted(tmp_path):
    """Multiple docs files are returned in sorted order."""
    import shutil
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    (repo / "docs" / "kb" / "zebra.md").write_text("# Zebra\n", encoding="utf-8")
    (repo / "docs" / "kb" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    h = _load_harvest()
    docs = h.harvest_docs_present(repo)
    assert docs == sorted(docs)
    assert "zebra.md" in docs
    assert "alpha.md" in docs


def test_harvest_docs_present_missing_dir_returns_empty(tmp_path):
    """Missing docs/kb/ → []."""
    h = _load_harvest()
    docs = h.harvest_docs_present(tmp_path)
    assert docs == []


def test_harvest_docs_present_only_direct_children(tmp_path):
    """Only *.md directly under docs/kb/ are returned (no subdirs)."""
    import shutil
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    subdir = repo / "docs" / "kb" / "sub"
    subdir.mkdir()
    (subdir / "nested.md").write_text("# Nested\n", encoding="utf-8")
    h = _load_harvest()
    docs = h.harvest_docs_present(repo)
    assert "nested.md" not in docs
    assert "architecture.md" in docs


def test_harvest_docs_present_bare_filenames():
    """Results are bare filenames, not full paths."""
    h = _load_harvest()
    docs = h.harvest_docs_present(FIXTURE_REPO)
    for d in docs:
        assert "/" not in d
        assert "\\" not in d


# ---------------------------------------------------------------------------
# harvest_identity
# ---------------------------------------------------------------------------

def test_harvest_identity_name_from_pyproject():
    """name is read from pyproject.toml [project] name."""
    h = _load_harvest()
    ident = h.harvest_identity(FIXTURE_REPO)
    assert ident["name"] == "fixturetool"


def test_harvest_identity_language_python():
    """language is 'python' when pyproject.toml is present."""
    h = _load_harvest()
    ident = h.harvest_identity(FIXTURE_REPO)
    assert ident["language"] == "python"


def test_harvest_identity_keys_present():
    """All five expected keys are present in the identity dict."""
    h = _load_harvest()
    ident = h.harvest_identity(FIXTURE_REPO)
    for key in ("name", "repo_url", "branch", "primary_binary", "language"):
        assert key in ident, f"missing key: {key}"


def test_harvest_identity_git_fields_none_without_git(tmp_path):
    """repo_url and branch are None when git is unavailable (no .git dir)."""
    import shutil
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    h = _load_harvest()
    ident = h.harvest_identity(repo)
    # fixture has no .git dir → git fields must be None
    assert ident["repo_url"] is None
    assert ident["branch"] is None


def test_harvest_identity_git_fields_populated_with_real_repo(tmp_path):
    """repo_url and branch are populated from git when a .git dir is present."""
    import shutil
    import subprocess as _sp
    repo = tmp_path / "repo"
    shutil.copytree(str(FIXTURE_REPO), str(repo))
    # Initialize a real git repo with a remote
    _sp.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _sp.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    _sp.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    _sp.run(["git", "remote", "add", "origin", "https://github.com/example/myrepo.git"],
            cwd=repo, check=True, capture_output=True)
    h = _load_harvest()
    ident = h.harvest_identity(repo)
    # repo_url must have trailing .git stripped; branch must be "main"
    assert ident["repo_url"] == "https://github.com/example/myrepo", (
        f"Expected clean URL without .git, got: {ident['repo_url']!r}"
    )
    assert ident["branch"] == "main", f"Expected branch 'main', got: {ident['branch']!r}"


def test_harvest_identity_missing_manifests_returns_none_name(tmp_path):
    """Empty repo with no manifest files → name is None."""
    h = _load_harvest()
    ident = h.harvest_identity(tmp_path)
    assert ident["name"] is None


# ---------------------------------------------------------------------------
# harvest_structured
# ---------------------------------------------------------------------------

def test_harvest_structured_keys():
    """harvest_structured returns the expected top-level keys."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    for key in ("name", "group", "head_sha", "identity", "docs_present", "adrs"):
        assert key in result, f"missing key: {key}"


def test_harvest_structured_passthrough_values():
    """name, group, head_sha are passed through unchanged."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    assert result["name"] == "fixturetool"
    assert result["group"] == "1.0-dev"
    assert result["head_sha"] == "abc1234"


def test_harvest_structured_docs_present_correct():
    """docs_present in structured output matches harvest_docs_present."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    assert result["docs_present"] == ["architecture.md"]


def test_harvest_structured_adrs_non_empty():
    """adrs list in structured output has at least one item for the fixture repo."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    assert len(result["adrs"]) >= 1


def test_harvest_structured_identity_embedded():
    """identity sub-dict is embedded with at least the name key."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    assert isinstance(result["identity"], dict)
    assert result["identity"]["name"] == "fixturetool"


# ---------------------------------------------------------------------------
# Determinism golden test
# ---------------------------------------------------------------------------

def test_harvest_deterministic():
    """Harvesting the fixture repo twice yields byte-identical JSON output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    r2 = h.harvest_structured(FIXTURE_REPO, "fixturetool", "1.0-dev", "abc1234")
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# harvest_argparse — Task 4
# ---------------------------------------------------------------------------

FIXTURE_CLI_ARGPARSE = FIXTURE_REPO / "src" / "cli_argparse.py"
FIXTURE_CLI_CLICK = FIXTURE_REPO / "src" / "cli_click.py"


def test_harvest_argparse_returns_two_items():
    """cli_argparse.py has 2 subparsers (run, check) → list of length 2."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    assert len(items) == 2


def test_harvest_argparse_command_names():
    """Subcommand names are 'run' and 'check'."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    commands = {it["command"] for it in items}
    assert commands == {"run", "check"}


def test_harvest_argparse_expected_slugs():
    """Slug for 'run' is 'run' and for 'check' is 'check'."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    by_cmd = {it["command"]: it for it in items}
    assert by_cmd["run"]["slug"] == "run"
    assert by_cmd["check"]["slug"] == "check"


def test_harvest_argparse_flags_include_dry_run():
    """--dry-run flag is present in at least one item's flags list."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    all_flags = [f for it in items for f in it["flags"]]
    assert "--dry-run" in all_flags


def test_harvest_argparse_flags_include_verbose():
    """--verbose flag is present in at least one item's flags list."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    all_flags = [f for it in items for f in it["flags"]]
    assert "--verbose" in all_flags


def test_harvest_argparse_item_shape():
    """Each item has the required keys: slug, command, invocation, flags."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    for it in items:
        assert "slug" in it
        assert "command" in it
        assert "invocation" in it
        assert "flags" in it
        assert isinstance(it["flags"], list)


def test_harvest_argparse_invocation_includes_command():
    """invocation contains the subcommand name."""
    h = _load_harvest()
    items = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    by_cmd = {it["command"]: it for it in items}
    assert "run" in by_cmd["run"]["invocation"]
    assert "check" in by_cmd["check"]["invocation"]


def test_harvest_argparse_no_cli_returns_empty(tmp_path):
    """A Python file with no ArgumentParser usage returns []."""
    h = _load_harvest()
    py_file = tmp_path / "noop.py"
    py_file.write_text("x = 1 + 2\nprint(x)\n", encoding="utf-8")
    items = h.harvest_argparse(py_file)
    assert items == []


def test_harvest_argparse_syntax_error_returns_empty(tmp_path):
    """A file with a Python syntax error returns [] (never fatal)."""
    h = _load_harvest()
    bad = tmp_path / "broken.py"
    bad.write_text("def foo(\n  x = :\n", encoding="utf-8")
    items = h.harvest_argparse(bad)
    assert items == []


def test_harvest_argparse_syntax_error_logs_to_stderr(tmp_path, capsys):
    """A syntax error is logged to stderr (AC: '[] + a stderr log')."""
    h = _load_harvest()
    bad = tmp_path / "broken.py"
    bad.write_text("def foo(\n  x = :\n", encoding="utf-8")
    h.harvest_argparse(bad)
    captured = capsys.readouterr()
    assert captured.err != ""


def test_harvest_argparse_no_subparser_returns_one_item(tmp_path):
    """ArgumentParser with no add_parser calls → 1 item with command = stem, slug derived."""
    h = _load_harvest()
    py_file = tmp_path / "my_tool.py"
    py_file.write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--output', help='output file')\n",
        encoding="utf-8",
    )
    items = h.harvest_argparse(py_file)
    assert len(items) == 1
    assert items[0]["command"] == "my_tool"       # stem, not slug
    assert items[0]["slug"] == "my-tool"          # slugified
    assert "--output" in items[0]["flags"]


def test_harvest_argparse_deterministic():
    """Harvesting cli_argparse.py twice yields identical output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    r2 = h.harvest_argparse(FIXTURE_CLI_ARGPARSE)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# harvest_click_typer — Task 4
# ---------------------------------------------------------------------------


def test_harvest_click_typer_returns_one_item():
    """cli_click.py has one @click.command (deploy) → list of length 1."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert len(items) == 1


def test_harvest_click_typer_command_name():
    """Command name is 'deploy'."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert items[0]["command"] == "deploy"


def test_harvest_click_typer_flag_env():
    """--env option is extracted into flags."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert "--env" in items[0]["flags"]


def test_harvest_click_typer_flag_force():
    """--force option is extracted into flags."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert "--force" in items[0]["flags"]


def test_harvest_click_typer_item_shape():
    """Item has the required keys: slug, command, invocation, flags."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    it = items[0]
    assert "slug" in it
    assert "command" in it
    assert "invocation" in it
    assert "flags" in it
    assert isinstance(it["flags"], list)


def test_harvest_click_typer_slug_deploy():
    """Slug for 'deploy' command is 'deploy'."""
    h = _load_harvest()
    items = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert items[0]["slug"] == "deploy"


def test_harvest_click_typer_no_cli_returns_empty(tmp_path):
    """A file with no @click.command returns []."""
    h = _load_harvest()
    py_file = tmp_path / "plain.py"
    py_file.write_text("def helper():\n    return 42\n", encoding="utf-8")
    items = h.harvest_click_typer(py_file)
    assert items == []


def test_harvest_click_typer_syntax_error_returns_empty(tmp_path):
    """A file with a Python syntax error returns [] (never fatal)."""
    h = _load_harvest()
    bad = tmp_path / "broken.py"
    bad.write_text("@click.command(\ndef foo(:\n", encoding="utf-8")
    items = h.harvest_click_typer(bad)
    assert items == []


def test_harvest_click_typer_syntax_error_logs_to_stderr(tmp_path, capsys):
    """A syntax error is logged to stderr (AC: '[] + a stderr log')."""
    h = _load_harvest()
    bad = tmp_path / "broken.py"
    bad.write_text("@click.command(\ndef foo(:\n", encoding="utf-8")
    h.harvest_click_typer(bad)
    captured = capsys.readouterr()
    assert captured.err != ""


def test_harvest_click_typer_deterministic():
    """Harvesting cli_click.py twice yields identical output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    r2 = h.harvest_click_typer(FIXTURE_CLI_CLICK)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# harvest_make — Task 5
# ---------------------------------------------------------------------------

FIXTURE_MAKEFILE = FIXTURE_REPO / "Makefile"


def test_harvest_make_returns_two_items():
    """Fixture Makefile has 2 real targets (build, test) → list of length 2."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    assert len(items) == 2


def test_harvest_make_slugs():
    """Slugs are 'make-build' and 'make-test'."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    slugs = {it["slug"] for it in items}
    assert slugs == {"make-build", "make-test"}


def test_harvest_make_build_has_summary():
    """build target has summary 'Build the thing' from the ## doc comment above it."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    by_slug = {it["slug"]: it for it in items}
    assert by_slug["make-build"]["summary"] == "Build the thing"


def test_harvest_make_phony_excluded():
    """.PHONY line is not parsed as a target."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    slugs = {it["slug"] for it in items}
    assert "make-.phony" not in slugs
    # .PHONY starts with '.', the ^[a-zA-Z] anchor excludes it
    for it in items:
        assert not it["slug"].startswith("make-.")


def test_harvest_make_command_field():
    """command field is 'make <target>'."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    for it in items:
        assert it["command"].startswith("make ")


def test_harvest_make_invocation_field():
    """invocation field equals command."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    for it in items:
        assert it["invocation"] == it["command"]


def test_harvest_make_flags_empty_list():
    """flags is always an empty list for make targets."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    for it in items:
        assert it["flags"] == []


def test_harvest_make_item_shape():
    """Each item has keys: slug, command, invocation, flags, summary."""
    h = _load_harvest()
    items = h.harvest_make(FIXTURE_REPO)
    for it in items:
        for key in ("slug", "command", "invocation", "flags", "summary"):
            assert key in it, f"missing key '{key}' in {it}"


def test_harvest_make_no_makefile_returns_empty(tmp_path):
    """Repo with no Makefile → []."""
    h = _load_harvest()
    items = h.harvest_make(tmp_path)
    assert items == []


def test_harvest_make_variable_assignment_excluded(tmp_path):
    """Lines like 'VAR:=value' must NOT be parsed as targets."""
    h = _load_harvest()
    mk = tmp_path / "Makefile"
    mk.write_text("VAR:=hello\nall:\n\t@echo done\n", encoding="utf-8")
    items = h.harvest_make(tmp_path)
    slugs = {it["slug"] for it in items}
    assert "make-var" not in slugs
    assert "make-all" in slugs


def test_harvest_make_deterministic():
    """Harvesting the fixture Makefile twice yields identical output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_make(FIXTURE_REPO)
    r2 = h.harvest_make(FIXTURE_REPO)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# harvest_npm — Task 5
# ---------------------------------------------------------------------------


def test_harvest_npm_returns_two_items():
    """Fixture package.json has 2 scripts (dev, lint) → list of length 2."""
    h = _load_harvest()
    items = h.harvest_npm(FIXTURE_REPO)
    assert len(items) == 2


def test_harvest_npm_slugs():
    """Slugs are 'npm-dev' and 'npm-lint'."""
    h = _load_harvest()
    items = h.harvest_npm(FIXTURE_REPO)
    slugs = {it["slug"] for it in items}
    assert slugs == {"npm-dev", "npm-lint"}


def test_harvest_npm_command_field():
    """command field is 'npm run <key>'."""
    h = _load_harvest()
    items = h.harvest_npm(FIXTURE_REPO)
    for it in items:
        assert it["command"].startswith("npm run ")


def test_harvest_npm_invocation_field():
    """invocation matches command."""
    h = _load_harvest()
    items = h.harvest_npm(FIXTURE_REPO)
    for it in items:
        assert it["invocation"] == it["command"]


def test_harvest_npm_item_shape():
    """Each item has keys: slug, command, invocation."""
    h = _load_harvest()
    items = h.harvest_npm(FIXTURE_REPO)
    for it in items:
        for key in ("slug", "command", "invocation"):
            assert key in it, f"missing key '{key}' in {it}"


def test_harvest_npm_no_package_json_returns_empty(tmp_path):
    """Repo with no package.json → []."""
    h = _load_harvest()
    items = h.harvest_npm(tmp_path)
    assert items == []


def test_harvest_npm_malformed_json_returns_empty(tmp_path):
    """Malformed package.json → [] (never raise)."""
    h = _load_harvest()
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    items = h.harvest_npm(tmp_path)
    assert items == []


def test_harvest_npm_malformed_json_logs_stderr(tmp_path, capsys):
    """Malformed package.json is reported to stderr."""
    h = _load_harvest()
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    h.harvest_npm(tmp_path)
    captured = capsys.readouterr()
    assert captured.err != ""


def test_harvest_npm_no_scripts_key_returns_empty(tmp_path):
    """package.json without a 'scripts' key → []."""
    h = _load_harvest()
    (tmp_path / "package.json").write_text('{"name": "foo"}', encoding="utf-8")
    items = h.harvest_npm(tmp_path)
    assert items == []


def test_harvest_npm_deterministic():
    """Harvesting the fixture package.json twice yields identical output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_npm(FIXTURE_REPO)
    r2 = h.harvest_npm(FIXTURE_REPO)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# harvest_errors — Task 5
# ---------------------------------------------------------------------------


def test_harvest_errors_contains_fooerror():
    """harvest_errors on fixture repo finds FooError class (code='FooError')."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    codes = {it["code"] for it in items}
    assert "FooError" in codes


def test_harvest_errors_fooerror_message():
    """FooError item has message 'Foo failed.' (first line of docstring)."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    by_code = {it["code"]: it for it in items}
    assert by_code["FooError"]["message"] == "Foo failed."


def test_harvest_errors_contains_exit2():
    """harvest_errors on fixture repo finds sys.exit(2) → code='exit-2'."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    codes = {it["code"] for it in items}
    assert "exit-2" in codes


def test_harvest_errors_fooerror_slug():
    """FooError slug is 'fooerror' (kebab of lowercase class name)."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    by_code = {it["code"]: it for it in items}
    assert by_code["FooError"]["slug"] == "fooerror"


def test_harvest_errors_exit2_slug():
    """exit-2 slug is 'exit-2'."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    by_code = {it["code"]: it for it in items}
    assert by_code["exit-2"]["slug"] == "exit-2"


def test_harvest_errors_item_shape():
    """Each item has keys: slug, code, message, trigger, fix."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    for it in items:
        for key in ("slug", "code", "message", "trigger", "fix"):
            assert key in it, f"missing key '{key}' in {it}"


def test_harvest_errors_deterministic_fields_empty():
    """trigger and fix are empty strings (deterministic — no interpretation)."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    for it in items:
        assert it["trigger"] == ""
        assert it["fix"] == ""


def test_harvest_errors_dedup_by_slug():
    """No duplicate slugs in the output."""
    h = _load_harvest()
    items = h.harvest_errors(FIXTURE_REPO)
    slugs = [it["slug"] for it in items]
    assert len(slugs) == len(set(slugs))


def test_harvest_errors_no_python_files_returns_empty(tmp_path):
    """Repo with no .py files → []."""
    h = _load_harvest()
    items = h.harvest_errors(tmp_path)
    assert items == []


def test_harvest_errors_syntax_error_file_skipped(tmp_path):
    """A .py file with a syntax error is skipped; valid files still processed."""
    h = _load_harvest()
    good = tmp_path / "good.py"
    good.write_text(
        "import sys\nclass BarError(Exception):\n    \"\"\"Bar failed.\"\"\"\n",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(\n  x = :\n", encoding="utf-8")
    items = h.harvest_errors(tmp_path)
    codes = {it["code"] for it in items}
    assert "BarError" in codes  # good file was processed


def test_harvest_errors_syntax_error_logs_stderr(tmp_path, capsys):
    """A syntax-error .py file is reported to stderr."""
    h = _load_harvest()
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(\n  x = :\n", encoding="utf-8")
    h.harvest_errors(tmp_path)
    captured = capsys.readouterr()
    assert captured.err != ""


def test_harvest_errors_deterministic():
    """Harvesting the fixture repo twice yields identical error output."""
    import json
    h = _load_harvest()
    r1 = h.harvest_errors(FIXTURE_REPO)
    r2 = h.harvest_errors(FIXTURE_REPO)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# Task 6 — harvest_structured extension + main()
# ---------------------------------------------------------------------------

import json
import os
import subprocess
import tempfile


def test_harvest_structured_includes_cli_key():
    """harvest_structured now returns a 'cli' key."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    assert "cli" in result


def test_harvest_structured_includes_errors_key():
    """harvest_structured now returns an 'errors' key."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    assert "errors" in result


def test_harvest_structured_includes_harvest_counts_key():
    """harvest_structured now returns a 'harvest_counts' key."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    assert "harvest_counts" in result


def test_harvest_structured_cli_completeness():
    """cli list includes commands from ALL parsers (argparse, click, make, npm)."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    slugs = {it["slug"] for it in result["cli"]}
    # argparse subcommands
    assert "run" in slugs
    assert "check" in slugs
    # click command
    assert "deploy" in slugs
    # make targets
    assert "make-build" in slugs
    assert "make-test" in slugs
    # npm scripts
    assert "npm-dev" in slugs
    assert "npm-lint" in slugs


def test_harvest_structured_cli_all_have_flags_key():
    """Every cli item has a 'flags' key (even npm/make which don't natively carry one)."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    for it in result["cli"]:
        assert "flags" in it, f"missing 'flags' on cli item: {it}"
        assert isinstance(it["flags"], list)


def test_harvest_structured_cli_dedup_by_slug():
    """No duplicate slugs in the assembled cli list."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    slugs = [it["slug"] for it in result["cli"]]
    assert len(slugs) == len(set(slugs))


def test_harvest_structured_cli_argparse_invocation_repo_relative():
    """Argparse cli items' invocation uses a path relative to the repo root (e.g. 'python src/...')."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    by_slug = {it["slug"]: it for it in result["cli"]}
    # 'run' comes from src/cli_argparse.py — relative path from repo root
    assert by_slug["run"]["invocation"].startswith("python src/")


def test_harvest_structured_harvest_counts_match_lengths():
    """harvest_counts.cli == len(cli), similarly for errors and adrs."""
    h = _load_harvest()
    result = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    counts = result["harvest_counts"]
    assert counts["cli"] == len(result["cli"])
    assert counts["errors"] == len(result["errors"])
    assert counts["adrs"] == len(result["adrs"])


def test_harvest_structured_full_deterministic():
    """harvest_structured (with cli/errors/harvest_counts) is byte-identical across two runs."""
    h = _load_harvest()
    r1 = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    r2 = h.harvest_structured(FIXTURE_REPO, "fixturetool", "3.0-work", "deadbee")
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# --- main() via subprocess ---

def _run_main(tmp_path, extra_args=None):
    """Run kb-harvest.py main() via subprocess; return (returncode, cache_file_path)."""
    cache_dir = tmp_path / "scout-cache"
    cache_dir.mkdir()
    cmd = [
        sys.executable, str(HARVEST_SCRIPT),
        "--repo", str(FIXTURE_REPO),
        "--name", "fixturetool",
        "--group", "3.0-work",
        "--head-sha", "deadbee",
        "--cache", str(cache_dir),
    ]
    if extra_args:
        cmd.extend(extra_args)
    env = dict(os.environ)
    env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    return result, cache_dir / "fixturetool.json"


def test_main_creates_cache_file(tmp_path):
    """main() creates <cache>/fixturetool.json."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert cache_file.exists()


def test_main_cache_file_valid_json(tmp_path):
    """The written cache file is valid JSON."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_main_structured_keys_written(tmp_path):
    """Structured keys (name, group, head_sha, identity, docs_present, adrs, cli, errors, harvest_counts) are present."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    for key in ("name", "group", "head_sha", "identity", "docs_present", "adrs", "cli", "errors", "harvest_counts"):
        assert key in data, f"missing key: {key}"


def test_main_cli_completeness(tmp_path):
    """main() writes all expected CLI slugs from all parsers."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    slugs = {it["slug"] for it in data["cli"]}
    for expected in ("run", "check", "deploy", "make-build", "make-test", "npm-dev", "npm-lint"):
        assert expected in slugs, f"slug '{expected}' missing from cli"


def test_main_all_cli_have_flags_key(tmp_path):
    """Every cli item in the written JSON has a 'flags' key."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    for it in data["cli"]:
        assert "flags" in it


def test_main_argparse_invocation_relative(tmp_path):
    """Argparse items' invocation starts with 'python src/'."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    by_slug = {it["slug"]: it for it in data["cli"]}
    assert by_slug["run"]["invocation"].startswith("python src/")


def test_main_harvest_counts_match_lengths(tmp_path):
    """harvest_counts values match the length of their respective arrays."""
    proc, cache_file = _run_main(tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    counts = data["harvest_counts"]
    assert counts["cli"] == len(data["cli"])
    assert counts["errors"] == len(data["errors"])
    assert counts["adrs"] == len(data["adrs"])


def test_main_prose_keys_preserved(tmp_path):
    """Prose keys already in the cache file survive main() overwrite."""
    cache_dir = tmp_path / "scout-cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "fixturetool.json"
    # Pre-seed a prose key
    cache_file.write_text(
        json.dumps({"summary": "keep me", "next_action": "do stuff"}, indent=2),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    cmd = [
        sys.executable, str(HARVEST_SCRIPT),
        "--repo", str(FIXTURE_REPO),
        "--name", "fixturetool",
        "--group", "3.0-work",
        "--head-sha", "deadbee",
        "--cache", str(cache_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data.get("summary") == "keep me"
    assert data.get("next_action") == "do stuff"


def test_main_byte_identical_on_two_runs(tmp_path):
    """Running main() twice produces byte-identical JSON output."""
    # Use a shared cache dir so both runs target the same file
    cache_dir = tmp_path / "scout-cache"
    cache_dir.mkdir()
    env = dict(os.environ)
    env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    cmd = [
        sys.executable, str(HARVEST_SCRIPT),
        "--repo", str(FIXTURE_REPO),
        "--name", "fixturetool",
        "--group", "3.0-work",
        "--head-sha", "deadbee",
        "--cache", str(cache_dir),
    ]
    cache_file = cache_dir / "fixturetool.json"

    proc1 = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    assert proc1.returncode == 0, proc1.stderr
    content1 = cache_file.read_bytes()

    proc2 = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    assert proc2.returncode == 0, proc2.stderr
    content2 = cache_file.read_bytes()

    assert content1 == content2


# --- Parser isolation (in-process — monkeypatching requires same process) ---

def test_main_parser_exception_still_writes_file():
    """If harvest_npm raises, the file is still written with cli=[] or partial."""
    h = _load_harvest()

    # Monkeypatch harvest_npm to raise
    original_npm = h.harvest_npm
    h.harvest_npm = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected"))

    try:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "scout-cache"
            cache_dir.mkdir()
            argv = [
                "--repo", str(FIXTURE_REPO),
                "--name", "fixturetool",
                "--group", "3.0-work",
                "--head-sha", "deadbee",
                "--cache", str(cache_dir),
            ]
            rc = h.main(argv)
            cache_file = cache_dir / "fixturetool.json"
            assert cache_file.exists(), "file must be written even when a parser fails"
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # No npm-* slugs since harvest_npm raised
            slugs = {it["slug"] for it in data.get("cli", [])}
            assert "npm-dev" not in slugs
            assert "npm-lint" not in slugs
    finally:
        h.harvest_npm = original_npm


def test_main_parser_exception_other_parsers_still_run():
    """When harvest_npm raises, the other parsers (argparse, click, make) still produce results."""
    h = _load_harvest()

    original_npm = h.harvest_npm
    h.harvest_npm = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected"))

    try:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "scout-cache"
            cache_dir.mkdir()
            argv = [
                "--repo", str(FIXTURE_REPO),
                "--name", "fixturetool",
                "--group", "3.0-work",
                "--head-sha", "deadbee",
                "--cache", str(cache_dir),
            ]
            h.main(argv)
            cache_file = cache_dir / "fixturetool.json"
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            slugs = {it["slug"] for it in data.get("cli", [])}
            # argparse, click, make still succeeded
            assert "run" in slugs
            assert "deploy" in slugs
            assert "make-build" in slugs
    finally:
        h.harvest_npm = original_npm


# ---------------------------------------------------------------------------
# iter_source_py — venv / build / cache pruning (Task: blast-radius fix)
# ---------------------------------------------------------------------------


def test_iter_source_py_excludes_venv(tmp_path):
    """iter_source_py does not yield files under .venv/."""
    first_party = tmp_path / "src" / "tool.py"
    first_party.parent.mkdir(parents=True)
    first_party.write_text("x = 1\n", encoding="utf-8")

    venv_pkg = tmp_path / ".venv" / "Lib" / "site-packages" / "evil.py"
    venv_pkg.parent.mkdir(parents=True)
    venv_pkg.write_text("import argparse\n", encoding="utf-8")

    h = _load_harvest()
    yielded = list(h.iter_source_py(tmp_path))
    assert first_party in yielded
    assert venv_pkg not in yielded


def test_iter_source_py_excludes_egg_info(tmp_path):
    """iter_source_py does not descend into *.egg-info directories."""
    real = tmp_path / "mypackage" / "code.py"
    real.parent.mkdir(parents=True)
    real.write_text("x = 1\n", encoding="utf-8")

    egg_file = tmp_path / "mypackage.egg-info" / "top_level.py"
    egg_file.parent.mkdir(parents=True)
    egg_file.write_text("y = 2\n", encoding="utf-8")

    h = _load_harvest()
    yielded = list(h.iter_source_py(tmp_path))
    assert real in yielded
    assert egg_file not in yielded


def test_iter_source_py_excludes_pycache(tmp_path):
    """iter_source_py does not descend into __pycache__ directories."""
    real = tmp_path / "app.py"
    real.write_text("x = 1\n", encoding="utf-8")

    cache_file = tmp_path / "__pycache__" / "app.cpython-311.pyc.py"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("compiled = True\n", encoding="utf-8")

    h = _load_harvest()
    yielded = list(h.iter_source_py(tmp_path))
    assert real in yielded
    assert cache_file not in yielded


def test_venv_cli_excluded_from_assemble_cli(tmp_path):
    """CLI items from .venv site-packages are NOT included; first-party items are.

    The decoy has an argparse parser with a unique prog name ('evilcmd').
    The first-party file has two argparse subcommands ('sync', 'report').
    Asserts: first-party slugs present, decoy slug absent, total count == 2.
    """
    # First-party CLI file
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser(prog='main')\n"
        "sp = p.add_subparsers(dest='cmd')\n"
        "sp.add_parser('sync', help='Sync data')\n"
        "sp.add_parser('report', help='Generate report')\n",
        encoding="utf-8",
    )

    # Vendored decoy under .venv
    decoy_dir = tmp_path / ".venv" / "Lib" / "site-packages" / "evildep"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "evil.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser(prog='evilcmd')\n"
        "p.add_argument('--flag')\n",
        encoding="utf-8",
    )

    h = _load_harvest()
    items = h._assemble_cli(tmp_path)
    slugs = {it["slug"] for it in items}

    assert "sync" in slugs, "first-party 'sync' subcommand must be present"
    assert "report" in slugs, "first-party 'report' subcommand must be present"
    assert "evilcmd" not in slugs, "vendored 'evilcmd' must be excluded"
    assert len(items) == 2, f"expected exactly 2 CLI items, got {len(items)}: {slugs}"


def test_venv_error_class_excluded_from_harvest_errors(tmp_path):
    """Error classes from .venv site-packages are NOT included in harvest_errors.

    The decoy defines FooVendorError under .venv; the first-party file defines
    RealAppError. Asserts: RealAppError present, FooVendorError absent.
    """
    # First-party error class
    (tmp_path / "errors.py").write_text(
        "class RealAppError(Exception):\n    \"\"\"Real application error.\"\"\"\n",
        encoding="utf-8",
    )

    # Vendored decoy under .venv
    decoy_dir = tmp_path / ".venv" / "Lib" / "site-packages" / "evildep"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "evil.py").write_text(
        "class FooVendorError(Exception):\n    \"\"\"Vendored error — must not appear.\"\"\"\n",
        encoding="utf-8",
    )

    h = _load_harvest()
    items = h.harvest_errors(tmp_path)
    codes = {it["code"] for it in items}

    assert "RealAppError" in codes, "first-party error class must be harvested"
    assert "FooVendorError" not in codes, "vendored error class must be excluded"


# ---------------------------------------------------------------------------
# Task C3 — lineage keys: advances / phase / milestones
# ---------------------------------------------------------------------------


def test_harvest_main_stamps_sidecar_lineage(tmp_path):
    """main() stamps advances/phase/milestones from project-edges.yaml into the cache."""
    vault = tmp_path / "vault"
    meta = vault / "00-meta"
    cache = meta / "scout-cache"
    cache.mkdir(parents=True)
    (meta / "project-edges.yaml").write_text(
        "demo:\n"
        "  advances: career\n"
        "  phase: build\n"
        "  milestones: [\"MVP|build|done\"]\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")

    h = _load_harvest()
    rc = h.main([
        "--repo", str(repo),
        "--name", "demo",
        "--group", "2.0-career",
        "--head-sha", "abc123",
        "--cache", str(cache),
    ])
    assert rc == 0, "main() should return 0"

    data = json.loads((cache / "demo.json").read_text(encoding="utf-8"))
    assert data["advances"] == "career"
    assert data["phase"] == "build"
    assert data["milestones"] == [{"title": "MVP", "phase": "build", "status": "done"}]


def test_harvest_main_absent_sidecar_defaults(tmp_path):
    """main() writes null/empty defaults when the project is absent from project-edges.yaml."""
    vault = tmp_path / "vault"
    cache = vault / "00-meta" / "scout-cache"
    cache.mkdir(parents=True)
    # No project-edges.yaml at all
    repo = tmp_path / "repo"
    repo.mkdir()

    h = _load_harvest()
    h.main([
        "--repo", str(repo),
        "--name", "x",
        "--group", "1.0-dev",
        "--head-sha", "s",
        "--cache", str(cache),
    ])

    d = json.loads((cache / "x.json").read_text(encoding="utf-8"))
    assert d["advances"] is None
    assert d["phase"] is None
    assert d["milestones"] == []


def test_harvest_main_vault_override(tmp_path):
    """--vault overrides the default cache.parent.parent derivation."""
    # cache is placed in an unrelated dir that does NOT have vault structure
    cache = tmp_path / "unrelated" / "scout-cache"
    cache.mkdir(parents=True)

    # The real vault lives at a different path
    vault = tmp_path / "real-vault"
    meta = vault / "00-meta"
    meta.mkdir(parents=True)
    (meta / "project-edges.yaml").write_text(
        "proj:\n"
        "  advances: career-growth\n"
        "  phase: ship\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    h = _load_harvest()
    rc = h.main([
        "--repo", str(repo),
        "--name", "proj",
        "--group", "1.0-dev",
        "--head-sha", "sha1",
        "--cache", str(cache),
        "--vault", str(vault),
    ])
    assert rc == 0
    d = json.loads((cache / "proj.json").read_text(encoding="utf-8"))
    assert d["advances"] == "career-growth"
    assert d["phase"] == "ship"


def test_harvest_lineage_keys_are_in_structured_keys():
    """Verify advances/phase/milestones ARE in _STRUCTURED_KEYS (so prose-merge never drops them)."""
    h = _load_harvest()
    assert "advances" in h._STRUCTURED_KEYS
    assert "phase" in h._STRUCTURED_KEYS
    assert "milestones" in h._STRUCTURED_KEYS


# ---------------------------------------------------------------------------
# harvest_artifacts
# ---------------------------------------------------------------------------

def test_harvest_artifacts_empty_repo(tmp_path):
    """A repo with no artifacts returns all false/zero."""
    h = _load_harvest()
    result = h.harvest_artifacts(tmp_path)
    assert result == {
        "readme_index_exists": False,
        "plan_file_exists": False,
        "decision_count": 0,
    }


def test_harvest_artifacts_readme_index(tmp_path):
    """Detects .readme/_index.md when present."""
    readme_dir = tmp_path / ".readme"
    readme_dir.mkdir()
    (readme_dir / "_index.md").write_text("# Index", encoding="utf-8")
    h = _load_harvest()
    result = h.harvest_artifacts(tmp_path)
    assert result["readme_index_exists"] is True
    assert result["plan_file_exists"] is False
    assert result["decision_count"] == 0


def test_harvest_artifacts_plan_file(tmp_path):
    """Detects active/plan/PLAN.md when present."""
    plan_dir = tmp_path / "active" / "plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "PLAN.md").write_text("# Plan", encoding="utf-8")
    h = _load_harvest()
    result = h.harvest_artifacts(tmp_path)
    assert result["plan_file_exists"] is True
    assert result["readme_index_exists"] is False
    assert result["decision_count"] == 0


def test_harvest_artifacts_decision_count(tmp_path):
    """Counts *.md files in active/decisions/."""
    dec_dir = tmp_path / "active" / "decisions"
    dec_dir.mkdir(parents=True)
    for i in range(3):
        (dec_dir / f"000{i}-adr.md").write_text("# ADR", encoding="utf-8")
    h = _load_harvest()
    result = h.harvest_artifacts(tmp_path)
    assert result["decision_count"] == 3
    assert result["readme_index_exists"] is False
    assert result["plan_file_exists"] is False


def test_harvest_artifacts_all_present(tmp_path):
    """All artifacts detected when all are present."""
    (tmp_path / ".readme").mkdir()
    (tmp_path / ".readme" / "_index.md").write_text("# Index", encoding="utf-8")
    plan_dir = tmp_path / "active" / "plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "PLAN.md").write_text("# Plan", encoding="utf-8")
    dec_dir = tmp_path / "active" / "decisions"
    dec_dir.mkdir(parents=True)
    (dec_dir / "0001-foo.md").write_text("# ADR", encoding="utf-8")
    (dec_dir / "0002-bar.md").write_text("# ADR", encoding="utf-8")
    h = _load_harvest()
    result = h.harvest_artifacts(tmp_path)
    assert result == {
        "readme_index_exists": True,
        "plan_file_exists": True,
        "decision_count": 2,
    }


def test_harvest_artifacts_in_structured_keys():
    """Verify 'artifacts' is in _STRUCTURED_KEYS."""
    h = _load_harvest()
    assert "artifacts" in h._STRUCTURED_KEYS


def test_harvest_structured_includes_artifacts(tmp_path):
    """harvest_structured() output always includes an 'artifacts' key."""
    h = _load_harvest()
    result = h.harvest_structured(tmp_path, "test-proj", "1.0-dev", "abc1234")
    assert "artifacts" in result
    assert "readme_index_exists" in result["artifacts"]
    assert "plan_file_exists" in result["artifacts"]
    assert "decision_count" in result["artifacts"]
