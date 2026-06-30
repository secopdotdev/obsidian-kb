"""Tests for kb-normalize.py — idempotent planning-artifact normalizer.

All tests are hermetic: they build synthetic repo dirs under tmp_path.
No real repo dependency, no network, no git shell-out.
Import pattern: importlib (hyphenated filename).
"""

import importlib.util
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loader (hyphen-safe)
# ---------------------------------------------------------------------------

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"
_SCRIPT = _SKILL / "kb-normalize.py"


def _load():
    """Load kb-normalize.py via importlib (hyphen-safe)."""
    spec = importlib.util.spec_from_file_location("kb_normalize", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


mod = _load()
analyze_repo = mod.analyze_repo
apply_plan = mod.apply_plan
render_human = mod.render_human
render_json = mod.render_json


# ---------------------------------------------------------------------------
# Helper: build a minimal repo fixture
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1. Frontmatter migration — bold **Status:** format
# ---------------------------------------------------------------------------

def test_frontmatter_migration_bold_status(tmp_path):
    """File with bold **Status:** / **Date:** inline metadata → needs-review migration."""
    _write(
        tmp_path / "active" / "decisions" / "ADR-0001-secrets.md",
        "# Use ESO + OpenBao\n\n**Status:** Accepted  \n**Date:** 2026-06-10\n\n## Context\n\nBody text.\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 1
    a = fm_actions[0]
    assert a.confidence == "needs-review"
    assert "type: adr" in a.detail["proposed_frontmatter"]
    assert "status: accepted" in a.detail["proposed_frontmatter"]
    assert a.detail["extracted_date"] == "2026-06-10"
    assert a.detail["extracted_title"] == "Use ESO + OpenBao"


def test_frontmatter_migration_status_normalised(tmp_path):
    """Status value 'in-progress' is normalised to 'active'."""
    _write(
        tmp_path / "active" / "plan" / "my-plan.md",
        "# My Plan\n\n**Status:** in-progress\n\nBody.\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert any("status: active" in a.detail["proposed_frontmatter"] for a in fm_actions)


# ---------------------------------------------------------------------------
# 2. Frontmatter migration — blockquote · separator format
# ---------------------------------------------------------------------------

def test_frontmatter_migration_blockquote_format(tmp_path):
    """File with > Status: Draft · Date: 2026-06-10 · ... blockquote → needs-review migration."""
    _write(
        tmp_path / "active" / "plan" / "PLAN.md",
        "# k3s-bootstrap Plan\n\n> Status: Draft · Date: 2026-06-10 · Architecture: SPEC.md\n\n## Build Order\n\nContent.\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 1
    a = fm_actions[0]
    assert "type: plan" in a.detail["proposed_frontmatter"]
    assert "status: draft" in a.detail["proposed_frontmatter"]
    assert a.detail["extracted_date"] == "2026-06-10"


def test_frontmatter_migration_blockquote_status_extracted(tmp_path):
    """Status from blockquote · format is correctly extracted (not missed)."""
    _write(
        tmp_path / "active" / "plan" / "SPEC.md",
        "# Design Spec\n\n> Status: Draft · Date: 2026-06-10 · Home: Q:/repo\n\n## Content\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    # Must detect the blockquote status
    assert len(fm_actions) == 1
    assert fm_actions[0].detail["extracted_status"] == "Draft"


# ---------------------------------------------------------------------------
# 3. Frontmatter migration — idempotency (already-YAML file → no-op)
# ---------------------------------------------------------------------------

def test_frontmatter_migration_idempotent_yaml_present(tmp_path):
    """File with valid YAML frontmatter containing type: → no migration action."""
    _write(
        tmp_path / "active" / "plan" / "already-done.md",
        "---\ntype: plan\ntitle: \"Already Done\"\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n# Already Done\n\nBody.\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 0, "file with complete YAML frontmatter must not be re-proposed"


def test_frontmatter_migration_yaml_without_type_is_proposed(tmp_path):
    """File with YAML frontmatter but NO type: field → still proposed for migration."""
    _write(
        tmp_path / "active" / "plan" / "partial.md",
        "---\ntitle: \"Partial\"\nstatus: draft\n---\n\n# Partial\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 1


# ---------------------------------------------------------------------------
# 4. ADR move — docs/superpowers/decisions → active/decisions/
# ---------------------------------------------------------------------------

def test_adr_move_from_docs_superpowers(tmp_path):
    """ADR in docs/superpowers/decisions/ → plan move to active/decisions/NNNN-slug.md."""
    _write(
        tmp_path / "docs" / "superpowers" / "decisions" / "0001-secrets.md",
        "# ADR 0001: Use ESO\n\n**Status:** Accepted\n",
    )
    plan = analyze_repo(tmp_path)
    move_actions = [a for a in plan.actions if a.kind == "adr_move"]
    assert len(move_actions) == 1
    a = move_actions[0]
    assert a.confidence == "deterministic"
    assert a.target_path.startswith("active/decisions/0001-")
    assert a.target_path.endswith(".md")
    assert "git mv" in a.detail["git_mv"]


def test_adr_move_nnnn_naming(tmp_path):
    """ADR moved from docs/adr/ → target filename uses NNNN- zero-padded format."""
    _write(
        tmp_path / "docs" / "adr" / "1-use-traefik.md",
        "# Use Traefik\n",
    )
    plan = analyze_repo(tmp_path)
    move_actions = [a for a in plan.actions if a.kind == "adr_move"]
    assert len(move_actions) == 1
    # Should be 0001-use-traefik.md (4-digit zero-padded)
    assert "0001" in move_actions[0].target_path


# ---------------------------------------------------------------------------
# 5. Gate extraction — numbered gate headings
# ---------------------------------------------------------------------------

def test_gate_extraction_numbered_gates(tmp_path):
    """### Gate 1: heading → gate_extraction action (needs-review)."""
    content = (
        "---\ntype: plan\ntitle: \"My Plan\"\nstatus: draft\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# My Plan\n\n"
        "## Phase 1\n\nContent.\n\n"
        "### Gate 1: NetworkPolicy is enforced\n\n"
        "```bash\n"
        "kubectl get netpol -A\n"
        "# Expected: two policies per namespace\n"
        "```\n"
    )
    _write(tmp_path / "active" / "plan" / "plan.md", content)
    plan = analyze_repo(tmp_path)
    gate_actions = [a for a in plan.actions if a.kind == "gate_extraction"]
    assert len(gate_actions) == 1
    a = gate_actions[0]
    assert a.confidence == "needs-review"
    assert "Gate 1" in a.detail["heading"]
    assert a.target_path.startswith("active/gates/gate-")


def test_gate_extraction_go_no_go_heading(tmp_path):
    """## Go/No-Go heading → gate_extraction action."""
    content = (
        "---\ntype: plan\ntitle: \"Plan\"\nstatus: draft\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Plan\n\n## Go/No-Go Decision\n\n- [ ] Tests pass\n- [ ] Stakeholder sign-off\n"
    )
    _write(tmp_path / "active" / "plan" / "plan.md", content)
    plan = analyze_repo(tmp_path)
    gate_actions = [a for a in plan.actions if a.kind == "gate_extraction"]
    assert len(gate_actions) == 1
    a = gate_actions[0]
    assert a.detail["criteria_count"] == 2
    assert "Tests pass" in a.detail["criteria"]


def test_gate_extraction_criteria_extracted(tmp_path):
    """Gate section with - [ ] bullets → criteria extracted correctly."""
    content = (
        "---\ntype: plan\ntitle: \"Plan\"\nstatus: draft\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Plan\n\n## Pre-implementation Gate\n\n"
        "- [ ] Unit tests pass\n"
        "- [ ] Security review done\n"
        "- [ ] ADR accepted\n\n"
        "## Next Section\n\nMore content.\n"
    )
    _write(tmp_path / "active" / "plan" / "plan.md", content)
    plan = analyze_repo(tmp_path)
    gate_actions = [a for a in plan.actions if a.kind == "gate_extraction"]
    assert len(gate_actions) == 1
    assert gate_actions[0].detail["criteria_count"] == 3
    assert "Unit tests pass" in gate_actions[0].detail["criteria"]
    assert "Security review done" in gate_actions[0].detail["criteria"]


def test_gate_extraction_no_criteria_no_crash(tmp_path):
    """Gate heading with no - [ ] items → action emitted with criteria_count=0, no crash."""
    content = (
        "---\ntype: plan\ntitle: \"Plan\"\nstatus: draft\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Plan\n\n### Gate 1: Verify\n\nSome prose only, no checklist.\n"
    )
    _write(tmp_path / "active" / "plan" / "plan.md", content)
    plan = analyze_repo(tmp_path)
    gate_actions = [a for a in plan.actions if a.kind == "gate_extraction"]
    assert len(gate_actions) == 1
    assert gate_actions[0].detail["criteria_count"] == 0


# ---------------------------------------------------------------------------
# 6. Scaffold — empty repo → scaffold plan
# ---------------------------------------------------------------------------

def test_scaffold_empty_repo(tmp_path):
    """Repo with no active/plan/ or active/decisions/ → scaffold actions for each dir."""
    # Put an unrelated file so repo isn't completely empty
    _write(tmp_path / "README.md", "# My Repo\n")
    plan = analyze_repo(tmp_path)
    scaffold_actions = [a for a in plan.actions if a.kind == "scaffold"]
    # Should propose plan, decisions, gates dirs
    targets = {a.detail["create_dir"] for a in scaffold_actions}
    assert "active/plan" in targets
    assert "active/decisions" in targets
    assert all(a.confidence == "deterministic" for a in scaffold_actions)


def test_scaffold_idempotent_when_dirs_exist(tmp_path):
    """Repo with active/plan/ and active/decisions/ already present → no scaffold actions."""
    (tmp_path / "active" / "plan").mkdir(parents=True)
    (tmp_path / "active" / "decisions").mkdir(parents=True)
    plan = analyze_repo(tmp_path)
    scaffold_actions = [a for a in plan.actions if a.kind == "scaffold"]
    assert len(scaffold_actions) == 0


# ---------------------------------------------------------------------------
# 7. --apply scaffold (deterministic, no git)
# ---------------------------------------------------------------------------

def test_apply_scaffold_creates_gitkeep(tmp_path):
    """apply_plan on an empty repo creates .gitkeep files in active/ dirs."""
    _write(tmp_path / "README.md", "# Empty Repo\n")
    plan = analyze_repo(tmp_path)
    scaffold_actions = [a for a in plan.actions if a.kind == "scaffold"]
    assert len(scaffold_actions) > 0
    results = apply_plan(plan, dry_run=False)
    # The .gitkeep files must now exist.
    for a in scaffold_actions:
        gitkeep = tmp_path / a.detail["create_file"]
        assert gitkeep.exists(), f"expected {gitkeep} to be created"
    assert any("created" in r for r in results)


def test_apply_scaffold_idempotent(tmp_path):
    """Applying scaffold twice is safe (second apply finds dirs already exist)."""
    _write(tmp_path / "README.md", "# Empty Repo\n")
    plan = analyze_repo(tmp_path)
    apply_plan(plan, dry_run=False)
    # Second analyze+apply must not crash and yields an empty scaffold list.
    plan2 = analyze_repo(tmp_path)
    scaffold2 = [a for a in plan2.actions if a.kind == "scaffold"]
    assert len(scaffold2) == 0


# ---------------------------------------------------------------------------
# 8. Fully-normalised repo → empty plan (end-to-end idempotency)
# ---------------------------------------------------------------------------

def test_fully_normalised_repo_empty_plan(tmp_path):
    """A repo where all files already have valid YAML frontmatter yields an empty plan."""
    (tmp_path / "active" / "plan").mkdir(parents=True)
    (tmp_path / "active" / "decisions").mkdir(parents=True)
    _write(
        tmp_path / "active" / "plan" / "01-bootstrap-plan.md",
        "---\ntype: plan\ntitle: \"Bootstrap Plan\"\nstatus: active\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Bootstrap Plan\n\nContent with no gate headings.\n",
    )
    _write(
        tmp_path / "active" / "decisions" / "0001-use-eso.md",
        "---\ntype: adr\ntitle: \"Use ESO\"\nstatus: accepted\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Use ESO\n\nBody.\n",
    )
    plan = analyze_repo(tmp_path)
    assert len(plan.actions) == 0, (
        f"expected empty plan for normalised repo, got: {[a.kind for a in plan.actions]}"
    )


# ---------------------------------------------------------------------------
# 9. JSON output round-trips cleanly
# ---------------------------------------------------------------------------

def test_json_output_round_trips(tmp_path):
    """render_json produces valid JSON with expected top-level keys."""
    import json as _json
    _write(
        tmp_path / "active" / "decisions" / "ADR-0001-test.md",
        "# Test ADR\n\n**Status:** Proposed\n**Date:** 2026-03-01\n\nBody.\n",
    )
    plan = analyze_repo(tmp_path)
    j = render_json(plan)
    data = _json.loads(j)
    assert "repo" in data
    assert "actions" in data
    assert "summary" in data
    assert "idempotency_note" in data


# ---------------------------------------------------------------------------
# 10. Type inference from directory path
# ---------------------------------------------------------------------------

def test_type_inferred_from_decisions_dir(tmp_path):
    """File in active/decisions/ → inferred type is 'adr'."""
    _write(
        tmp_path / "active" / "decisions" / "my-decision.md",
        "# My Decision\n\n**Status:** Accepted\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 1
    assert fm_actions[0].detail["inferred_type"] == "adr"


def test_type_inferred_from_plan_dir(tmp_path):
    """File in active/plan/ → inferred type is 'plan'."""
    _write(
        tmp_path / "active" / "plan" / "my-plan.md",
        "# My Plan\n\n**Status:** Draft\n",
    )
    plan = analyze_repo(tmp_path)
    fm_actions = [a for a in plan.actions if a.kind == "frontmatter_migration"]
    assert len(fm_actions) == 1
    assert fm_actions[0].detail["inferred_type"] == "plan"


# ---------------------------------------------------------------------------
# 11. Human render: plan with actions produces non-empty output
# ---------------------------------------------------------------------------

def test_human_render_nonempty(tmp_path):
    """render_human on a plan with actions returns a non-empty string."""
    _write(
        tmp_path / "active" / "plan" / "plan.md",
        "# Plan\n\n**Status:** Draft\n\nBody.\n",
    )
    plan = analyze_repo(tmp_path)
    out = render_human(plan)
    assert "kb-normalize plan" in out
    assert "FRONTMATTER MIGRATION" in out


# ---------------------------------------------------------------------------
# 12. Multiple gate headings in one file
# ---------------------------------------------------------------------------

def test_multiple_gates_in_one_file(tmp_path):
    """A file with 3 gate headings → 3 gate_extraction actions with distinct targets."""
    content = (
        "---\ntype: plan\ntitle: \"Multi Gate Plan\"\nstatus: draft\n"
        "created: 2026-01-01\nupdated: 2026-01-01\ntags: []\nrelated: []\n---\n\n"
        "# Multi Gate Plan\n\n"
        "### Gate 1: Preflight\n\n- [ ] OS configured\n\n"
        "### Gate 2: Install\n\n- [ ] Node Ready\n\n"
        "### Gate 3: Network\n\n- [ ] DNS resolves\n"
    )
    _write(tmp_path / "active" / "plan" / "plan.md", content)
    plan = analyze_repo(tmp_path)
    gate_actions = [a for a in plan.actions if a.kind == "gate_extraction"]
    assert len(gate_actions) == 3
    targets = {a.target_path for a in gate_actions}
    assert len(targets) == 3  # all distinct


# ---------------------------------------------------------------------------
# 13. .planning/ is never touched (GSD-owned exclusion)
# ---------------------------------------------------------------------------

def test_planning_dir_excluded(tmp_path):
    """.planning/ contents are never analysed (GSD-owned)."""
    _write(
        tmp_path / ".planning" / "adr" / "0001-some-adr.md",
        "# GSD ADR\n\n**Status:** Draft\n",
    )
    # Also add an active/plan/ dir so scaffold doesn't fire.
    (tmp_path / "active" / "plan").mkdir(parents=True)
    (tmp_path / "active" / "decisions").mkdir(parents=True)
    plan = analyze_repo(tmp_path)
    # .planning/ should produce zero actions of any kind
    for a in plan.actions:
        assert ".planning" not in a.source_path
        assert ".planning" not in a.target_path
