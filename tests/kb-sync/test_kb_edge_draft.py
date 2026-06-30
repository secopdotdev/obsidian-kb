"""Tests for kb-edge-draft.py — review-gated edge-drafter (sidecar-based apply).

All tests are hermetic (tmp_path synthetic fixtures; no real vault dependency).

Design change from earlier version:
    `apply --apply` now writes/merges into 00-meta/project-edges.yaml (the durable
    sidecar) rather than rewriting project card frontmatter.  Cards are GENERATED
    files that are silently overwritten on each /kb-sync run — writing to them is
    not durable.  The sidecar is operator-owned and persists across regenerations.
"""
from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import kb-edge-draft.py via importlib (hyphenated name).
# ---------------------------------------------------------------------------
SKILL = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "kb_edge_draft", SKILL / "kb-edge-draft.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ED = _load_module()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CARD_FM_TEMPLATE = """\
---
# --- generator-owned ---
type: project
title: "{title}"
group: "{group}"
# --- operator-owned ---
status: active
rag-flag: green
blocker-severity: ""
blockers: []
next-action: "needs-triage"
next-command: ""
notes: ""
---

# {title}

Some body text.
"""

_CARD_WITH_REQUIRES_TEMPLATE = """\
---
type: project
title: "{title}"
group: "{group}"
status: active
requires: [{existing_requires}]
goal: {goal}
---

# {title}
"""


def _write_card(vault: Path, group: str, stem: str, title: str) -> Path:
    """Write a minimal project card to 02-projects/<group>/<stem>.md."""
    card_dir = vault / "02-projects" / group
    card_dir.mkdir(parents=True, exist_ok=True)
    path = card_dir / f"{stem}.md"
    text = _CARD_FM_TEMPLATE.format(title=title, group=group)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _write_card_with_requires(
    vault: Path,
    group: str,
    stem: str,
    title: str,
    existing_requires: list[str],
    goal: bool = False,
) -> Path:
    """Write a project card that already has requires and goal fields."""
    card_dir = vault / "02-projects" / group
    card_dir.mkdir(parents=True, exist_ok=True)
    path = card_dir / f"{stem}.md"
    req_str = ", ".join(f'"{r}"' for r in existing_requires)
    text = _CARD_WITH_REQUIRES_TEMPLATE.format(
        title=title,
        group=group,
        existing_requires=req_str,
        goal=str(goal).lower(),
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _minimal_graph(nodes: list[dict], edges: list[dict]) -> dict:
    return {"nodes": nodes, "edges": edges}


def _sidecar_path(vault: Path) -> Path:
    return vault / "00-meta" / "project-edges.yaml"


# ---------------------------------------------------------------------------
# Tests: parse_frontmatter helpers
# ---------------------------------------------------------------------------

class TestFmHelpers:
    def test_fm_field_scalar(self):
        text = "---\ntitle: \"hello\"\ngroup: 1.0-dev\n---\nbody"
        assert ED._fm_field(text, "title") == "hello"
        assert ED._fm_field(text, "group") == "1.0-dev"

    def test_fm_field_absent(self):
        text = "---\ntitle: test\n---\nbody"
        assert ED._fm_field(text, "requires") is None

    def test_fm_field_list_returns_empty_string(self):
        text = "---\nrequires: [a, b]\n---\n"
        # Returns "" (signals list present) rather than None.
        result = ED._fm_field(text, "requires")
        assert result == ""

    def test_parse_block_list_raw_inline(self):
        text = '---\nrequires: ["alpha", "beta"]\n---\n'
        assert ED._parse_block_list_raw(text, "requires") == ["alpha", "beta"]

    def test_parse_block_list_raw_block_form(self):
        text = "---\nrequires:\n  - alpha\n  - beta\n---\n"
        assert ED._parse_block_list_raw(text, "requires") == ["alpha", "beta"]

    def test_parse_block_list_raw_empty(self):
        text = "---\nrequires: []\n---\n"
        assert ED._parse_block_list_raw(text, "requires") == []

    def test_parse_block_list_raw_absent(self):
        text = "---\ntitle: test\n---\n"
        assert ED._parse_block_list_raw(text, "requires") == []


# ---------------------------------------------------------------------------
# Tests: read_project_cards
# ---------------------------------------------------------------------------

class TestReadProjectCards:
    def test_reads_project_cards(self, tmp_path):
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")
        _write_card(vault, "1.1-dev-tools", "proj-b", "proj-b")

        cards = ED.read_project_cards(vault)
        titles = {c["title"] for c in cards}
        assert "proj-a" in titles
        assert "proj-b" in titles

    def test_skips_index_files(self, tmp_path):
        vault = tmp_path / "vault"
        idx_dir = vault / "02-projects"
        idx_dir.mkdir(parents=True)
        (idx_dir / "_INDEX.md").write_text(
            "---\ntype: moc\ntitle: \"Index\"\n---\n", encoding="utf-8", newline="\n"
        )
        _write_card(vault, "1.0-dev", "real-proj", "real-proj")

        cards = ED.read_project_cards(vault)
        assert all(c["title"] != "Index" for c in cards)
        assert any(c["title"] == "real-proj" for c in cards)

    def test_skips_non_project_type(self, tmp_path):
        vault = tmp_path / "vault"
        card_dir = vault / "02-projects" / "1.0-dev"
        card_dir.mkdir(parents=True)
        (card_dir / "gate-note.md").write_text(
            "---\ntype: gate\ntitle: \"some-gate\"\n---\n",
            encoding="utf-8", newline="\n",
        )
        _write_card(vault, "1.0-dev", "real-proj", "real-proj")

        cards = ED.read_project_cards(vault)
        assert all(c["title"] != "some-gate" for c in cards)

    def test_returns_group_and_path(self, tmp_path):
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "myproj", "myproj")

        cards = ED.read_project_cards(vault)
        assert len(cards) == 1
        assert cards[0]["group"] == "1.0-dev"
        assert cards[0]["path"] == "02-projects/1.0-dev/myproj.md"


# ---------------------------------------------------------------------------
# Tests: prepare (worksheet generation)
# ---------------------------------------------------------------------------

class TestPrepare:
    def test_worksheet_contains_all_projects(self, tmp_path):
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "alpha", "alpha")
        _write_card(vault, "1.0-dev", "beta", "beta")
        _write_card(vault, "1.1-dev-tools", "gamma", "gamma")

        out = tmp_path / "worksheet.md"
        ret = ED.cmd_prepare(vault, out)
        assert ret == 0
        assert out.exists()

        text = out.read_text(encoding="utf-8")
        assert "## alpha" in text
        assert "## beta" in text
        assert "## gamma" in text

    def test_worksheet_contains_slots(self, tmp_path):
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "alpha", "alpha")

        out = tmp_path / "worksheet.md"
        ED.cmd_prepare(vault, out)
        text = out.read_text(encoding="utf-8")

        assert "requires: []" in text
        assert "goal: false" in text
        assert "hints:" in text

    def test_worksheet_has_same_group_hint(self, tmp_path):
        """Sibling projects in the same group should appear in hints."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "alpha", "alpha")
        _write_card(vault, "1.0-dev", "beta", "beta")

        out = tmp_path / "worksheet.md"
        ED.cmd_prepare(vault, out)
        text = out.read_text(encoding="utf-8")

        # alpha's block should mention beta as a hint (sibling), and vice versa.
        sections = text.split("## ")
        alpha_block = next((s for s in sections if s.startswith("alpha")), "")
        assert "beta" in alpha_block, "alpha should hint at sibling beta"

        beta_block = next((s for s in sections if s.startswith("beta")), "")
        assert "alpha" in beta_block, "beta should hint at sibling alpha"

    def test_worksheet_has_gate_derived_hint(self, tmp_path):
        """Gate-ids from graph.json edges should appear in hints for connected projects."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        # Write a minimal graph.json that connects proj-a → gate-g1.
        graph = _minimal_graph(
            nodes=[
                {"id": "02-projects/1.0-dev/proj-a.md", "type": "project",
                 "label": "proj-a", "rag": None, "group": "1.0-dev",
                 "topo_rank": 0, "cycle_id": None, "goal": False,
                 "next": None, "blocker": None},
                {"id": "gate-g1", "type": "gate", "label": "gate-g1",
                 "rag": None, "group": None, "topo_rank": 0, "cycle_id": None,
                 "goal": False, "next": None, "blocker": None,
                 "status": "open", "blocking": True, "criteria_count": 0},
            ],
            edges=[
                {"s": "02-projects/1.0-dev/proj-a.md", "d": "gate-g1",
                 "t": "requires", "cycle": False, "brk": False},
            ],
        )
        graph_dir = vault / "00-meta"
        graph_dir.mkdir(parents=True)
        (graph_dir / "graph.json").write_text(
            json.dumps(graph), encoding="utf-8", newline="\n"
        )

        out = tmp_path / "worksheet.md"
        ED.cmd_prepare(vault, out)
        text = out.read_text(encoding="utf-8")

        # proj-a's hints should include gate-g1.
        sections = text.split("## ")
        proj_block = next((s for s in sections if s.startswith("proj-a")), "")
        assert "gate-g1" in proj_block

    def test_atomic_write_idempotent(self, tmp_path):
        """Running prepare twice produces identical output (byte-for-byte)."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "alpha", "alpha")
        out = tmp_path / "worksheet.md"

        ED.cmd_prepare(vault, out)
        first = out.read_text(encoding="utf-8")
        mtime1 = out.stat().st_mtime_ns

        ED.cmd_prepare(vault, out)
        second = out.read_text(encoding="utf-8")
        mtime2 = out.stat().st_mtime_ns

        assert first == second
        # Idempotent write skips the file — mtime unchanged.
        assert mtime1 == mtime2


# ---------------------------------------------------------------------------
# Tests: parse_worksheet
# ---------------------------------------------------------------------------

class TestParseWorksheet:
    def _ws(self, blocks: str) -> str:
        return "<!-- header -->\n\n" + blocks

    def test_parses_empty_requires(self):
        ws = self._ws(
            "## my-project\n\npath: 02-projects/1.0-dev/my-project.md\n"
            "group: 1.0-dev\nrequires: []\ngoal: false\nhints: none\n\n---\n"
        )
        entries = ED.parse_worksheet(ws)
        assert len(entries) == 1
        assert entries[0]["title"] == "my-project"
        assert entries[0]["requires"] == []
        assert entries[0]["goal"] is False

    def test_parses_filled_requires(self):
        ws = self._ws(
            '## proj-a\n\npath: 02-projects/1.0-dev/proj-a.md\n'
            'group: 1.0-dev\nrequires: ["proj-b", "gate-g0"]\n'
            "goal: true\nhints: proj-b\n\n---\n"
        )
        entries = ED.parse_worksheet(ws)
        assert len(entries) == 1
        e = entries[0]
        assert e["requires"] == ["proj-b", "gate-g0"]
        assert e["goal"] is True

    def test_hints_line_ignored(self):
        """hints: line must NOT appear in requires."""
        ws = self._ws(
            '## proj\n\npath: 02-projects/1.0-dev/proj.md\n'
            'group: 1.0-dev\nrequires: []\n'
            'goal: false\nhints: some-other-proj\n\n---\n'
        )
        entries = ED.parse_worksheet(ws)
        assert entries[0]["requires"] == []

    def test_parses_multiple_projects(self):
        ws = self._ws(
            "## alpha\n\npath: 02-projects/1.0-dev/alpha.md\n"
            "group: 1.0-dev\nrequires: []\ngoal: false\nhints: beta\n\n---\n\n"
            "## beta\n\npath: 02-projects/1.0-dev/beta.md\n"
            "group: 1.0-dev\nrequires: []\ngoal: false\nhints: alpha\n\n---\n"
        )
        entries = ED.parse_worksheet(ws)
        titles = [e["title"] for e in entries]
        assert "alpha" in titles
        assert "beta" in titles

    def test_bare_comma_separated_requires(self):
        """requires: a, b (no brackets) must split into ["a", "b"]."""
        ws = self._ws(
            "## proj\n\npath: 02-projects/1.0-dev/proj.md\n"
            "group: 1.0-dev\nrequires: dep-a, dep-b\n"
            "goal: false\nhints: none\n\n---\n"
        )
        entries = ED.parse_worksheet(ws)
        assert len(entries) == 1
        assert entries[0]["requires"] == ["dep-a", "dep-b"]

    def test_bare_single_requires(self):
        """requires: single-dep (no comma, no brackets) becomes ["single-dep"]."""
        ws = self._ws(
            "## proj\n\npath: 02-projects/1.0-dev/proj.md\n"
            "group: 1.0-dev\nrequires: single-dep\n"
            "goal: false\nhints: none\n\n---\n"
        )
        entries = ED.parse_worksheet(ws)
        assert entries[0]["requires"] == ["single-dep"]


# ---------------------------------------------------------------------------
# Tests: merge_requires
# ---------------------------------------------------------------------------

class TestMergeRequires:
    def test_union_appends_new(self):
        assert ED._merge_requires(["a"], ["b"]) == ["a", "b"]

    def test_dedupe_preserves_existing(self):
        assert ED._merge_requires(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_empty_existing(self):
        assert ED._merge_requires([], ["x", "y"]) == ["x", "y"]

    def test_empty_proposed(self):
        assert ED._merge_requires(["a"], []) == ["a"]

    def test_all_existing_no_op(self):
        assert ED._merge_requires(["a", "b"], ["a"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Tests: sidecar parsing and emission
# ---------------------------------------------------------------------------

class TestSidecarParsing:
    def test_parse_empty_sidecar(self):
        text = "# header\n"
        result = ED._parse_sidecar(text)
        assert result == {}

    def test_parse_single_entry(self):
        text = (
            "# header\n"
            "proj-a:\n"
            '  requires: ["dep-x", "dep-y"]\n'
            "  goal: true\n"
        )
        result = ED._parse_sidecar(text)
        assert "proj-a" in result
        entry = result["proj-a"]
        assert entry["requires"] == ["dep-x", "dep-y"]
        assert entry["goal"] is True

    def test_parse_goal_absent_is_false(self):
        text = (
            "proj-a:\n"
            '  requires: ["dep-x"]\n'
        )
        result = ED._parse_sidecar(text)
        assert result["proj-a"]["goal"] is False

    def test_parse_multiple_entries(self):
        text = (
            "02-projects/1.0-dev/alpha.md:\n"
            '  requires: ["beta"]\n'
            "\n"
            "02-projects/1.0-dev/beta.md:\n"
            '  requires: []\n'
            "  goal: true\n"
        )
        result = ED._parse_sidecar(text)
        assert len(result) == 2
        assert result["02-projects/1.0-dev/alpha.md"]["requires"] == ["beta"]
        assert result["02-projects/1.0-dev/beta.md"]["goal"] is True

    def test_emit_roundtrip(self):
        """Parse → emit → parse must produce identical data."""
        data = {
            "02-projects/1.0-dev/proj-a.md": {"requires": ["dep-x", "dep-y"], "goal": True},
            "02-projects/1.0-dev/proj-b.md": {"requires": [], "goal": False},
        }
        text = ED._emit_sidecar(data)
        parsed = ED._parse_sidecar(text)
        # proj-b has empty requires and goal=false — it IS emitted with requires:[]
        assert parsed["02-projects/1.0-dev/proj-a.md"]["requires"] == ["dep-x", "dep-y"]
        assert parsed["02-projects/1.0-dev/proj-a.md"]["goal"] is True
        assert parsed["02-projects/1.0-dev/proj-b.md"]["requires"] == []
        assert parsed["02-projects/1.0-dev/proj-b.md"]["goal"] is False

    def test_emit_sorted_keys(self):
        """Emitted sidecar must have keys in sorted order."""
        data = {
            "zzz-proj.md": {"requires": [], "goal": False},
            "aaa-proj.md": {"requires": [], "goal": False},
        }
        text = ED._emit_sidecar(data)
        aaa_pos = text.index("aaa-proj.md")
        zzz_pos = text.index("zzz-proj.md")
        assert aaa_pos < zzz_pos

    def test_emit_goal_false_omitted(self):
        """goal:false must NOT appear in the emitted sidecar (absent == false)."""
        data = {"proj.md": {"requires": ["dep"], "goal": False}}
        text = ED._emit_sidecar(data)
        assert "goal: false" not in text
        assert "goal: true" not in text

    def test_emit_idempotent(self):
        """Emitting twice from the same data produces byte-identical output."""
        data = {
            "02-projects/1.0-dev/proj-a.md": {"requires": ["dep-x"], "goal": True},
        }
        assert ED._emit_sidecar(data) == ED._emit_sidecar(data)


# ---------------------------------------------------------------------------
# Tests: apply dry-run (sidecar-based)
# ---------------------------------------------------------------------------

class TestApplyDryRun:
    def _make_worksheet(self, vault: Path, entries: list[dict]) -> Path:
        """Render a minimal worksheet from entry dicts."""
        lines = ["<!-- header -->\n"]
        for e in entries:
            req_str = ", ".join(f'"{r}"' for r in e.get("requires", []))
            lines.append(
                f"## {e['title']}\n\n"
                f"path: {e['path']}\n"
                f"group: {e.get('group', '')}\n"
                f"requires: [{req_str}]\n"
                f"goal: {str(e.get('goal', False)).lower()}\n"
                f"hints: (none)\n\n---\n\n"
            )
        ws_path = vault / "ws.md"
        ws_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return ws_path

    def test_dry_run_prints_diff_and_does_not_write(self, tmp_path, capsys):
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["proj-b"],
              "goal": False}],
        )
        ret = ED.cmd_apply(ws, vault, apply=False)
        assert ret == 0

        out = capsys.readouterr().out
        # diff should show proj-b being added
        assert "proj-b" in out

        # Sidecar must NOT be written on dry-run.
        assert not _sidecar_path(vault).exists()

    def test_dry_run_no_output_when_no_changes(self, tmp_path, capsys):
        """An all-empty worksheet produces no stdout output."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": [],
              "goal": False}],
        )
        ret = ED.cmd_apply(ws, vault, apply=False)
        assert ret == 0

        out = capsys.readouterr().out
        # Nothing printed to stdout (empty entries are skipped).
        assert out.strip() == ""


# ---------------------------------------------------------------------------
# Tests: apply --apply (sidecar writes)
# ---------------------------------------------------------------------------

class TestApplyApply:
    def _make_worksheet(self, vault: Path, entries: list[dict]) -> Path:
        lines = ["<!-- header -->\n"]
        for e in entries:
            req_str = ", ".join(f'"{r}"' for r in e.get("requires", []))
            lines.append(
                f"## {e['title']}\n\n"
                f"path: {e['path']}\n"
                f"group: {e.get('group', '')}\n"
                f"requires: [{req_str}]\n"
                f"goal: {str(e.get('goal', False)).lower()}\n"
                f"hints: (none)\n\n---\n\n"
            )
        ws_path = vault / "ws.md"
        ws_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return ws_path

    def test_apply_writes_sidecar(self, tmp_path):
        """apply --apply creates the sidecar with the approved requires."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["gate-g0"],
              "goal": False}],
        )
        ret = ED.cmd_apply(ws, vault, apply=True)
        assert ret == 0

        sidecar = _sidecar_path(vault)
        assert sidecar.exists(), "sidecar must be created on --apply"
        text = sidecar.read_text(encoding="utf-8")
        assert "gate-g0" in text

    def test_apply_card_frontmatter_never_touched(self, tmp_path):
        """The project card file must NOT be modified by apply."""
        vault = tmp_path / "vault"
        card_path = _write_card(vault, "1.0-dev", "proj-a", "proj-a")
        original_text = card_path.read_text(encoding="utf-8")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["gate-g0"],
              "goal": True}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        # Card must be byte-identical to the original.
        assert card_path.read_text(encoding="utf-8") == original_text, (
            "apply must never write to project card frontmatter (cards are GENERATED)"
        )

    def test_apply_merges_with_existing_sidecar(self, tmp_path):
        """Union semantics: existing sidecar requires are preserved; new items appended."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        # Pre-populate sidecar with an existing dep.
        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "proj-a:\n"
            '  requires: ["old-dep"]\n',
            encoding="utf-8", newline="\n",
        )

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["new-dep"],
              "goal": False}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        reqs = result["proj-a"]["requires"]
        assert "old-dep" in reqs
        assert "new-dep" in reqs

    def test_apply_dedupes_requires(self, tmp_path):
        """Already-present requires must not be duplicated in sidecar."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        # Pre-populate sidecar.
        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "proj-a:\n"
            '  requires: ["gate-g0"]\n',
            encoding="utf-8", newline="\n",
        )

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["gate-g0"],  # same as existing
              "goal": False}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        reqs = result["proj-a"]["requires"]
        assert reqs.count("gate-g0") == 1, "no duplicate requires in sidecar"

    def test_apply_sets_goal_in_sidecar(self, tmp_path):
        """goal: true should be written to sidecar when absent."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": [],
              "goal": True}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        sidecar = _sidecar_path(vault)
        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        assert result["proj-a"]["goal"] is True

    def test_apply_never_clobbers_existing_goal_true(self, tmp_path):
        """goal is monotonic: once true in sidecar, cannot be lowered to false."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        # Pre-populate sidecar with goal: true.
        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "proj-a:\n"
            '  requires: []\n'
            "  goal: true\n",
            encoding="utf-8", newline="\n",
        )

        # Worksheet proposes goal: false — must not overwrite existing true.
        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": [],
              "goal": False}],  # proposing false, but existing is true
        )
        # Entry has no requires and goal=false → skipped (no changes).
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        assert result["proj-a"]["goal"] is True, (
            "existing goal:true must not be clobbered by a worksheet goal:false"
        )

    def test_apply_idempotent_no_file_change(self, tmp_path):
        """Re-running --apply with same worksheet is a no-op (sidecar unchanged)."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")

        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["dep-x"],
              "goal": False}],
        )

        # First apply.
        ED.cmd_apply(ws, vault, apply=True)
        sidecar = _sidecar_path(vault)
        after_first = sidecar.read_text(encoding="utf-8")
        mtime1 = sidecar.stat().st_mtime_ns

        # Second apply — same worksheet.
        ED.cmd_apply(ws, vault, apply=True)
        after_second = sidecar.read_text(encoding="utf-8")
        mtime2 = sidecar.stat().st_mtime_ns

        assert after_first == after_second
        # Idempotent write skips the file — mtime unchanged.
        assert mtime1 == mtime2, (
            "apply is idempotent: sidecar must not be rewritten when content is unchanged"
        )

    def test_apply_preserves_projects_not_in_worksheet(self, tmp_path):
        """Projects already in the sidecar but absent from the worksheet must be preserved."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-a", "proj-a")
        _write_card(vault, "1.0-dev", "proj-b", "proj-b")

        # Sidecar has proj-b already.
        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "proj-b:\n"
            '  requires: ["some-existing-dep"]\n',
            encoding="utf-8", newline="\n",
        )

        # Worksheet only has proj-a.
        ws = self._make_worksheet(
            vault,
            [{"title": "proj-a",
              "path": "02-projects/1.0-dev/proj-a.md",
              "group": "1.0-dev",
              "requires": ["new-dep"],
              "goal": False}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        # proj-b must still be in the sidecar with its original data.
        assert "proj-b" in result
        assert result["proj-b"]["requires"] == ["some-existing-dep"]

    def test_apply_unresolvable_source_flagged_and_skipped(self, tmp_path, capsys):
        """Unresolvable card: flagged to stderr, nothing written, exit non-zero."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "real-proj", "real-proj")

        ws = self._make_worksheet(
            vault,
            [{"title": "ghost-proj",
              "path": "02-projects/1.0-dev/ghost-proj.md",  # does not exist
              "group": "1.0-dev",
              "requires": ["some-dep"],
              "goal": False}],
        )
        ret = ED.cmd_apply(ws, vault, apply=True)

        # Should return non-zero (error).
        assert ret != 0

        err = capsys.readouterr().err
        assert "FLAG" in err or "cannot resolve" in err.lower()

        # Sidecar must NOT be created (no valid entries were processed).
        assert not _sidecar_path(vault).exists()


# ---------------------------------------------------------------------------
# Tests: main() integration (argparse + full flow)
# ---------------------------------------------------------------------------

class TestMainIntegration:
    def _write_minimal_vault(self, tmp_path: Path) -> Path:
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "proj-alpha", "proj-alpha")
        _write_card(vault, "1.0-dev", "proj-beta", "proj-beta")
        return vault

    def test_main_prepare_creates_worksheet(self, tmp_path):
        """main() prepare subcommand writes the worksheet file."""
        vault = self._write_minimal_vault(tmp_path)
        out = tmp_path / "ws.md"
        ret = ED.main([
            "--vault", str(vault),
            "prepare",
            "--out", str(out),
        ])
        assert ret == 0
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "## proj-alpha" in text
        assert "## proj-beta" in text

    def test_main_apply_writes_sidecar(self, tmp_path):
        """main() apply --apply subcommand creates the sidecar."""
        vault = self._write_minimal_vault(tmp_path)

        # Build worksheet manually with a filled-in entry.
        ws_path = tmp_path / "ws.md"
        ws_path.write_text(
            "<!-- header -->\n\n"
            "## proj-alpha\n\n"
            "path: 02-projects/1.0-dev/proj-alpha.md\n"
            "group: 1.0-dev\n"
            'requires: ["proj-beta"]\n'
            "goal: false\n"
            "hints: (none)\n\n---\n\n",
            encoding="utf-8", newline="\n",
        )

        ret = ED.main([
            "--vault", str(vault),
            "apply", str(ws_path),
            "--apply",
        ])
        assert ret == 0

        sidecar = _sidecar_path(vault)
        assert sidecar.exists(), "sidecar must be created by main() apply --apply"
        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        assert "proj-beta" in result["proj-alpha"]["requires"]

    def test_main_prepare_then_apply_roundtrip(self, tmp_path):
        """prepare → manual edit → apply: sidecar has the edited values."""
        vault = self._write_minimal_vault(tmp_path)
        out = tmp_path / "ws.md"

        # Step 1: prepare.
        ED.main(["--vault", str(vault), "prepare", "--out", str(out)])
        ws_text = out.read_text(encoding="utf-8")

        # Step 2: simulate human edit — replace `requires: []` only in the
        # proj-alpha section (the header also contains `requires: []` in a comment
        # so we target only the section-level line by anchoring after the path).
        # Strategy: rebuild the text with the proj-alpha block edited.
        lines = ws_text.splitlines(keepends=True)
        in_alpha_section = False
        out_lines: list[str] = []
        for ln in lines:
            if ln.startswith("## proj-alpha"):
                in_alpha_section = True
            elif ln.startswith("## ") and in_alpha_section:
                in_alpha_section = False
            if in_alpha_section and ln.rstrip() == "requires: []":
                out_lines.append('requires: ["proj-beta"]\n')
                continue
            out_lines.append(ln)
        out.write_text("".join(out_lines), encoding="utf-8", newline="\n")

        # Step 3: apply.
        ret = ED.main(["--vault", str(vault), "apply", str(out), "--apply"])
        assert ret == 0

        sidecar = _sidecar_path(vault)
        assert sidecar.exists()
        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        # proj-alpha should have proj-beta in requires.
        alpha_entry = result.get("proj-alpha")
        assert alpha_entry is not None
        assert "proj-beta" in alpha_entry["requires"]


# ---------------------------------------------------------------------------
# NEW: Task 10 Part B — supersedes/partof round-trip safety in kb-edge-draft
# ---------------------------------------------------------------------------

class TestSidecarSupersedesparsing:
    """_parse_sidecar / _emit_sidecar must handle supersedes and partof keys."""

    def test_parse_supersedes_key(self):
        text = (
            "projbeta:\n"
            '  requires: ["projgamma"]\n'
            '  supersedes: ["ProjectOne"]\n'
        )
        result = ED._parse_sidecar(text)
        assert result["projbeta"]["supersedes"] == ["ProjectOne"]

    def test_parse_partof_key(self):
        text = (
            "childproj:\n"
            '  requires: []\n'
            '  partof: ["parentproj"]\n'
        )
        result = ED._parse_sidecar(text)
        assert result["childproj"]["partof"] == ["parentproj"]

    def test_parse_supersedes_absent_returns_empty_list(self):
        text = (
            "proj-a:\n"
            '  requires: ["dep-x"]\n'
        )
        result = ED._parse_sidecar(text)
        assert result["proj-a"].get("supersedes", []) == []

    def test_parse_partof_absent_returns_empty_list(self):
        text = (
            "proj-a:\n"
            '  requires: ["dep-x"]\n'
        )
        result = ED._parse_sidecar(text)
        assert result["proj-a"].get("partof", []) == []

    def test_emit_supersedes_when_non_empty(self):
        data = {
            "projbeta": {
                "requires": ["projgamma"],
                "supersedes": ["ProjectOne"],
                "partof": [],
                "goal": False,
            }
        }
        text = ED._emit_sidecar(data)
        assert "supersedes:" in text
        assert "ProjectOne" in text

    def test_emit_supersedes_omitted_when_empty(self):
        data = {
            "proj-a": {
                "requires": ["dep"],
                "supersedes": [],
                "partof": [],
                "goal": False,
            }
        }
        text = ED._emit_sidecar(data)
        assert "supersedes:" not in text

    def test_emit_partof_when_non_empty(self):
        data = {
            "childproj": {
                "requires": [],
                "supersedes": [],
                "partof": ["parentproj"],
                "goal": False,
            }
        }
        text = ED._emit_sidecar(data)
        assert "partof:" in text
        assert "parentproj" in text

    def test_emit_partof_omitted_when_empty(self):
        data = {
            "proj-a": {
                "requires": [],
                "supersedes": [],
                "partof": [],
                "goal": False,
            }
        }
        text = ED._emit_sidecar(data)
        assert "partof:" not in text

    def test_emit_roundtrip_preserves_supersedes_and_partof(self):
        """Parse → emit → parse produces identical supersedes and partof values."""
        data = {
            "projbeta": {
                "requires": ["projgamma"],
                "supersedes": ["ProjectOne"],
                "partof": ["some-parent"],
                "goal": False,
            }
        }
        text = ED._emit_sidecar(data)
        parsed = ED._parse_sidecar(text)
        assert parsed["projbeta"]["supersedes"] == ["ProjectOne"]
        assert parsed["projbeta"]["partof"] == ["some-parent"]
        assert parsed["projbeta"]["requires"] == ["projgamma"]


class TestApplyPreservesSupersedes:
    """apply --apply must NOT clobber operator-authored supersedes/partof when merging requires."""

    def _make_worksheet(self, vault: Path, entries: list[dict]) -> Path:
        lines = ["<!-- header -->\n"]
        for e in entries:
            req_str = ", ".join(f'"{r}"' for r in e.get("requires", []))
            lines.append(
                f"## {e['title']}\n\n"
                f"path: {e['path']}\n"
                f"group: {e.get('group', '')}\n"
                f"requires: [{req_str}]\n"
                f"goal: {str(e.get('goal', False)).lower()}\n"
                f"hints: (none)\n\n---\n\n"
            )
        ws_path = vault / "ws.md"
        ws_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return ws_path

    def test_apply_preserves_supersedes_after_requires_merge(self, tmp_path):
        """A sidecar with projbeta: {requires:[x], supersedes:[ProjectOne]},
        after apply adds a new requires, still has supersedes:[ProjectOne]."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "projbeta", "projbeta")

        # Pre-populate sidecar with supersedes.
        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "projbeta:\n"
            '  requires: ["projgamma"]\n'
            '  supersedes: ["ProjectOne"]\n',
            encoding="utf-8", newline="\n",
        )

        # Worksheet proposes a new requires — supersedes must survive.
        ws = self._make_worksheet(
            vault,
            [{"title": "projbeta",
              "path": "02-projects/1.0-dev/projbeta.md",
              "group": "1.0-dev",
              "requires": ["new-dep"],
              "goal": False}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        assert "ProjectOne" in result["projbeta"]["supersedes"], (
            "apply must preserve operator-authored supersedes; it was clobbered"
        )
        # Also verify the new require was merged.
        assert "new-dep" in result["projbeta"]["requires"]
        assert "projgamma" in result["projbeta"]["requires"]

    def test_apply_preserves_partof_after_requires_merge(self, tmp_path):
        """Operator-authored partof must survive an apply that adds a new requires."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "childproj", "childproj")

        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "childproj:\n"
            '  requires: []\n'
            '  partof: ["parentproj"]\n',
            encoding="utf-8", newline="\n",
        )

        ws = self._make_worksheet(
            vault,
            [{"title": "childproj",
              "path": "02-projects/1.0-dev/childproj.md",
              "group": "1.0-dev",
              "requires": ["new-req"],
              "goal": False}],
        )
        ED.cmd_apply(ws, vault, apply=True)

        result = ED._parse_sidecar(sidecar.read_text(encoding="utf-8"))
        assert "parentproj" in result["childproj"]["partof"], (
            "apply must preserve operator-authored partof; it was clobbered"
        )
        assert "new-req" in result["childproj"]["requires"]

    def test_apply_supersedes_preserved_across_idempotent_rerun(self, tmp_path):
        """Re-running apply (idempotent) must not lose supersedes on second run."""
        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "projbeta", "projbeta")

        sidecar = _sidecar_path(vault)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            "projbeta:\n"
            '  requires: ["projgamma"]\n'
            '  supersedes: ["ProjectOne"]\n',
            encoding="utf-8", newline="\n",
        )

        ws = self._make_worksheet(
            vault,
            [{"title": "projbeta",
              "path": "02-projects/1.0-dev/projbeta.md",
              "group": "1.0-dev",
              "requires": ["projgamma"],  # already present → no-op
              "goal": False}],
        )
        # Run apply twice; second run must be no-op and leave sidecar intact.
        ED.cmd_apply(ws, vault, apply=True)
        after_first = sidecar.read_text(encoding="utf-8")
        ED.cmd_apply(ws, vault, apply=True)
        after_second = sidecar.read_text(encoding="utf-8")

        # supersedes must survive both runs.
        result = ED._parse_sidecar(after_second)
        assert "ProjectOne" in result["projbeta"]["supersedes"], (
            "supersedes must not be lost after idempotent apply re-run"
        )
