"""Tests for kb-lineage-apply.py — TDD first, implementation follows.

Run:  py -3 -m pytest tests/test_kb_lineage_apply.py -v
"""
import importlib.util
import textwrap
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location(
        "kb_lineage_apply", Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-lineage-apply.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _sidecar(vault):
    spec = importlib.util.spec_from_file_location(
        "kb_graph", Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-graph.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m._read_sidecar(vault)


# ---------------------------------------------------------------------------
# 1. Basic write + other projects preserved byte-identically
# ---------------------------------------------------------------------------

def test_sets_lane_preserving_other_projects(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    p = meta / "project-edges.yaml"
    p.write_text(
        'projalpha:\n  requires: ["projgamma"]\n'
        'projbeta:\n  requires: ["projgamma"]\n', encoding="utf-8")
    m = _load()
    rc = m.apply_lineage(tmp_path, "projgamma", advances="shared", phase="harden",
                         milestones=[], requires=[])
    assert rc == 0
    txt = p.read_text(encoding="utf-8")
    assert "projalpha:" in txt and "projbeta:" in txt  # preserved
    sc = _sidecar(tmp_path)
    assert sc["projgamma"]["advances"] == "shared" and sc["projgamma"]["phase"] == "harden"
    # other projects unchanged
    assert sc["projalpha"]["requires"] == ["projgamma"]
    assert sc["projbeta"]["requires"] == ["projgamma"]


# ---------------------------------------------------------------------------
# 2. Enum validation — reject invalid lane, no partial write
# ---------------------------------------------------------------------------

def test_rejects_invalid_enum(tmp_path):
    (tmp_path / "00-meta").mkdir()
    m = _load()
    rc = m.apply_lineage(tmp_path, "x", advances="not-a-lane", phase="build",
                         milestones=[], requires=[])
    assert rc != 0  # no partial write
    # file should not have been created with bad data
    p = tmp_path / "00-meta" / "project-edges.yaml"
    if p.exists():
        assert "not-a-lane" not in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. Only-if-blank: human value wins when force=False
# ---------------------------------------------------------------------------

def test_does_not_overwrite_human_value_without_force(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text("x:\n  advances: career\n", encoding="utf-8")
    m = _load()
    m.apply_lineage(tmp_path, "x", advances="home", phase=None, milestones=[],
                    requires=[], force=False)
    assert _sidecar(tmp_path)["x"]["advances"] == "career"  # human value wins


# ---------------------------------------------------------------------------
# 4. force=True overwrites human value
# ---------------------------------------------------------------------------

def test_force_overwrites(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text("x:\n  advances: career\n", encoding="utf-8")
    m = _load()
    m.apply_lineage(tmp_path, "x", advances="home", phase=None, milestones=[],
                    requires=[], force=True)
    assert _sidecar(tmp_path)["x"]["advances"] == "home"


# ---------------------------------------------------------------------------
# 5. Milestones roundtrip: pipe-form serialized + re-parsed correctly
# ---------------------------------------------------------------------------

def test_milestones_roundtrip(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text("x:\n", encoding="utf-8")
    m = _load()
    m.apply_lineage(tmp_path, "x", advances=None, phase=None,
                    milestones=[{"title": "MVP", "phase": "build", "status": "done"},
                                 {"title": "Beta", "phase": None, "status": "todo"}],
                    requires=[])
    ms = _sidecar(tmp_path)["x"]["milestones"]
    assert ms == [{"title": "MVP", "phase": "build", "status": "done"},
                  {"title": "Beta", "phase": None, "status": "todo"}]


# ---------------------------------------------------------------------------
# 6. apply_project_advances: preserves other keys in objectives.yaml
# ---------------------------------------------------------------------------

def test_project_advances_preserves_other_keys(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "objectives.yaml").write_text(
        "objectives:\n  objective-a:\n    label: \"X\"\n    kind: ultimate\n"
        "project_advances:\n  projbeta: [\"objective-b\"]\n", encoding="utf-8")
    m = _load()
    rc = m.apply_project_advances(tmp_path, "projgamma", ["objective-a"])
    assert rc == 0
    txt = (meta / "objectives.yaml").read_text(encoding="utf-8")
    assert "objective-a" in txt and "projbeta:" in txt  # preserved
    # verify via kb-graph objectives reader
    spec = importlib.util.spec_from_file_location(
        "kb_graph2", Path(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-graph.py")
    kg = importlib.util.module_from_spec(spec); spec.loader.exec_module(kg)
    obj = kg._read_objectives(tmp_path)
    assert obj["project_advances"]["projgamma"] == ["objective-a"]
    assert obj["project_advances"]["projbeta"] == ["objective-b"]


# ---------------------------------------------------------------------------
# 7. Only-if-blank: requires list not wiped when empty arg passed (no force)
# ---------------------------------------------------------------------------

def test_requires_not_wiped_by_empty_arg(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text('x:\n  requires: ["a", "b"]\n', encoding="utf-8")
    m = _load()
    # Pass empty requires — only-if-blank means existing requires list is preserved
    m.apply_lineage(tmp_path, "x", advances="career", phase=None, milestones=[],
                    requires=[], force=False)
    sc = _sidecar(tmp_path)["x"]
    assert sc["requires"] == ["a", "b"]   # not wiped
    assert sc["advances"] == "career"      # new field set (was blank)


# ---------------------------------------------------------------------------
# 8. Preserves supersedes + requires when only setting phase
#    (extra requirement beyond the core tests — guards the re-emit path)
# ---------------------------------------------------------------------------

def test_preserves_supersedes_and_requires_when_setting_phase(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'x:\n  requires: ["a"]\n  supersedes: ["old"]\n', encoding="utf-8")
    m = _load()
    m.apply_lineage(tmp_path, "x", advances=None, phase="ship", milestones=[], requires=[])
    sc = _sidecar(tmp_path)["x"]
    assert sc["supersedes"] == ["old"] and sc["requires"] == ["a"] and sc["phase"] == "ship"


# ---------------------------------------------------------------------------
# 9. Preserves partof + goal when only setting phase (guards goal-emit path)
# ---------------------------------------------------------------------------

def test_preserves_partof_and_goal_when_setting_phase(tmp_path):
    meta = tmp_path / "00-meta"
    meta.mkdir()
    (meta / "project-edges.yaml").write_text(
        'x:\n  partof: ["parent"]\n  goal: true\n', encoding="utf-8")
    m = _load()
    rc = m.apply_lineage(tmp_path, "x", advances="career", phase="build",
                         milestones=[], requires=[])
    assert rc == 0
    sc = _sidecar(tmp_path)
    assert sc["x"]["partof"] == ["parent"]
    assert sc["x"]["goal"] is True
    assert sc["x"]["advances"] == "career" and sc["x"]["phase"] == "build"


# ---------------------------------------------------------------------------
# 10. Data-driven enums — (a) load from lineage-enums.yaml
# ---------------------------------------------------------------------------

def _write_enums(vault: Path, lanes: list[str], phases: list[str]) -> None:
    """Write a minimal lineage-enums.yaml to vault/00-meta/."""
    meta = vault / "00-meta"
    meta.mkdir(exist_ok=True)
    lines = ["lanes:"]
    for lane in lanes:
        lines.append(f"  - {lane}")
    lines.append("phases:")
    for phase in phases:
        lines.append(f"  - {phase}")
    (meta / "lineage-enums.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_enum_loads_from_file(tmp_path):
    """(a) Lanes and phases read from a written lineage-enums.yaml."""
    _write_enums(tmp_path, ["career", "home", "shared"], ["seed", "build"])
    (tmp_path / "00-meta" / "project-edges.yaml").write_text("", encoding="utf-8")
    m = _load()
    lanes, phases = m._load_enums(tmp_path)
    assert lanes == frozenset({"career", "home", "shared"})
    assert phases == frozenset({"seed", "build"})


# ---------------------------------------------------------------------------
# 11. Data-driven enums — (b) fallback when file is missing
# ---------------------------------------------------------------------------

def test_enum_fallback_on_missing_file(tmp_path):
    """(b) Missing lineage-enums.yaml triggers fallback; a known current lane validates."""
    # tmp_path has no 00-meta/lineage-enums.yaml at all
    m = _load()
    lanes, phases = m._load_enums(tmp_path)
    # Fallback set must contain the standard advance lanes
    assert "career" in lanes
    assert "home" in lanes
    assert "shared" in lanes
    assert "seed" in phases and "build" in phases and "harden" in phases and "ship" in phases


def test_apply_lineage_fallback_validates_known_lane(tmp_path):
    """(b) apply_lineage succeeds on a fallback lane when enums file is absent."""
    (tmp_path / "00-meta").mkdir()
    m = _load()
    rc = m.apply_lineage(tmp_path, "x", advances="career", phase="build",
                         milestones=[], requires=[])
    assert rc == 0


# ---------------------------------------------------------------------------
# 12. Data-driven enums — (c) NON-VACUITY: value only in file validates;
#     value absent from file + NOT in fallback rejects
# ---------------------------------------------------------------------------

def test_enum_file_extends_valid_set(tmp_path):
    """(c) Non-vacuity: a lane present in the file but NOT in the old hardcoded set validates.

    This test MUST FAIL if the loader ignores lineage-enums.yaml and falls back instead,
    because 'custom-lane-xyz' is deliberately absent from _LANES_FALLBACK.
    """
    _write_enums(tmp_path, ["career", "custom-lane-xyz"], ["seed", "build"])
    (tmp_path / "00-meta" / "project-edges.yaml").write_text("", encoding="utf-8")
    m = _load()
    # 'custom-lane-xyz' is not in the fallback constants — proves file is being read.
    rc = m.apply_lineage(tmp_path, "proj", advances="custom-lane-xyz", phase="seed",
                         milestones=[], requires=[])
    assert rc == 0, (
        "apply_lineage must accept a lane defined only in lineage-enums.yaml; "
        "if this fails, the loader is ignoring the file and using fallback instead"
    )


def test_enum_file_only_lane_not_in_fallback_rejects_when_absent(tmp_path):
    """(c) Counter-test: 'custom-lane-xyz' is rejected when NOT in the file (only in fallback check)."""
    # Write a file that does NOT include 'custom-lane-xyz'
    _write_enums(tmp_path, ["career", "home"], ["seed", "build"])
    (tmp_path / "00-meta" / "project-edges.yaml").write_text("", encoding="utf-8")
    m = _load()
    rc = m.apply_lineage(tmp_path, "proj", advances="custom-lane-xyz", phase="seed",
                         milestones=[], requires=[])
    assert rc != 0, (
        "apply_lineage must reject a lane absent from both the file and fallback constants"
    )


# ---------------------------------------------------------------------------
# 13. Data-driven enums — (d) invalid value still rejected
# ---------------------------------------------------------------------------

def test_enum_invalid_still_rejected_with_file(tmp_path):
    """(d) An enum-invalid value is rejected even when lineage-enums.yaml is present."""
    _write_enums(tmp_path, ["career", "home"], ["seed", "ship"])
    (tmp_path / "00-meta" / "project-edges.yaml").write_text("", encoding="utf-8")
    m = _load()
    rc = m.apply_lineage(tmp_path, "proj", advances="not-a-lane", phase="seed",
                         milestones=[], requires=[])
    assert rc != 0


# ---------------------------------------------------------------------------
# 14. Fallback on non-UTF-8 file (fix #1 — UnicodeDecodeError caught as ValueError)
# ---------------------------------------------------------------------------

def test_enum_fallback_on_non_utf8_file(tmp_path):
    """Non-UTF-8 bytes in enums file must trigger fallback, never raise.

    Without fix #1 (except OSError only), path.read_text(encoding='utf-8') raises
    UnicodeDecodeError (a ValueError subclass) which escapes the handler → crash.
    With fix #1 (except (OSError, ValueError)), the fallback constants are returned.
    """
    meta = tmp_path / "00-meta"
    meta.mkdir()
    # Write bytes that are invalid UTF-8 to the enums file.
    (meta / "lineage-enums.yaml").write_bytes(b"\xff\xfe invalid utf-8")
    m = _load()
    # Must not raise; must return fallback sets containing known lanes.
    lanes, phases = m._load_enums(tmp_path)
    assert "career" in lanes, (
        "Fallback lanes must include 'career'; got: " + repr(lanes)
    )
    assert "seed" in phases, (
        "Fallback phases must include 'seed'; got: " + repr(phases)
    )


# ---------------------------------------------------------------------------
# 15. Inline comment and quoted value parsing (fix #2 — strip comments + quotes)
# ---------------------------------------------------------------------------

def test_enum_inline_comment_and_quoted_value_parse_cleanly(tmp_path):
    """Inline comments and surrounding quotes must be stripped from item values.

    Without fix #2, '  - career  # current' parses to 'career  # current' (not 'career'),
    and '  - "home"' parses to '"home"' (not 'home') — both fail validation.
    """
    meta = tmp_path / "00-meta"
    meta.mkdir()
    # Write lanes with an inline comment and a quoted value.
    (meta / "lineage-enums.yaml").write_text(
        "lanes:\n"
        '  - career  # current primary lane\n'
        '  - "home"\n'
        "phases:\n"
        "  - seed\n"
        "  - 'build'\n",
        encoding="utf-8",
    )
    m = _load()
    lanes, phases = m._load_enums(tmp_path)
    assert "career" in lanes, (
        "Inline comment not stripped: expected 'career', got: " + repr(lanes)
    )
    assert "home" in lanes, (
        "Double-quotes not stripped: expected 'home', got: " + repr(lanes)
    )
    assert "build" in phases, (
        "Single-quotes not stripped: expected 'build', got: " + repr(phases)
    )
    # The raw un-stripped values must NOT appear in the set.
    assert "career  # current primary lane" not in lanes
    assert '"home"' not in lanes
    assert "'build'" not in phases
