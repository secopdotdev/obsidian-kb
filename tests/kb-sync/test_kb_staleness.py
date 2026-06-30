"""Tests for kb-staleness.py — KB staleness engine.

Strategy: use real throwaway git repos (tmp_path) to exercise the real git
binary.  No mocking of git.  Monkeypatching of internal helpers is used ONLY
to prove non-vacuity (see TestNonVacuity).

Test coverage:
    1. fresh      — documented sha == HEAD → state "fresh", drift_commits 0
    2. stale      — HEAD has advanced recently → state "stale"
    3. very_stale — HEAD advanced > STALE_DAYS ago → state "very_stale"
    4. unknown variants:
         a. path does not exist
         b. path is a dir but not a git repo
    5. NON-VACUITY PROOF — neutering the HEAD-vs-sha comparison must break the
       stale assertion (RED proof that the guard is real)
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load kb-staleness.py via importlib (hyphen in name; also keeps isolation)
# ---------------------------------------------------------------------------
_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "kb-sync"


def _load_staleness():
    spec = importlib.util.spec_from_file_location(
        "kb_staleness", _SKILL_DIR / "kb-staleness.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


KB_STALENESS = _load_staleness()


# ---------------------------------------------------------------------------
# Helpers — build throwaway git repos and fake vault cards
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args, env=None) -> str:
    """Run git in *cwd* and return stdout (stripped).  Raises on nonzero exit."""
    base_env: dict | None = None
    if env:
        import os
        base_env = {**os.environ, **env}
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=base_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed (rc={result.returncode}):\n"
            f"  stdout: {result.stdout!r}\n"
            f"  stderr: {result.stderr!r}"
        )
    return result.stdout.strip()


def _init_repo(repo_path: Path, *, branch: str = "main") -> None:
    """Create a new git repo at *repo_path* with safe local config."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", f"--initial-branch={branch}")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test User")
    _git(repo_path, "config", "commit.gpgsign", "false")


def _commit(repo_path: Path, message: str, *, date_offset_days: int = 0) -> str:
    """Create a commit (empty file change) and return its sha.

    *date_offset_days* < 0 back-dates the commit that many days into the past.
    Both GIT_COMMITTER_DATE and GIT_AUTHOR_DATE are set so `log --format=%ct`
    reports the intended timestamp.
    """
    # Create/modify a file so there's a real diff.
    marker = repo_path / "marker.txt"
    marker.write_text(f"{message}\n", encoding="utf-8")
    _git(repo_path, "add", "marker.txt")

    env: dict = {}
    if date_offset_days != 0:
        dt = datetime.now(timezone.utc) + timedelta(days=date_offset_days)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env["GIT_COMMITTER_DATE"] = ts
        env["GIT_AUTHOR_DATE"] = ts

    _git(repo_path, "commit", "-m", message, env=env or None)
    return _git(repo_path, "rev-parse", "HEAD")


def _write_card(vault_root: Path, group: str, name: str, path: str, sha: str) -> Path:
    """Write a minimal project card to <vault_root>/02-projects/<group>/<name>.md."""
    card_dir = vault_root / "02-projects" / group
    card_dir.mkdir(parents=True, exist_ok=True)
    card = card_dir / f"{name}.md"
    card.write_text(
        f"---\n"
        f"type: project\n"
        f"title: \"{name}\"\n"
        f"last-documented-sha: \"{sha}\"\n"
        f"path: '{path}'\n"
        f"---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return card


# ---------------------------------------------------------------------------
# 1. FRESH — HEAD == documented sha
# ---------------------------------------------------------------------------

class TestFresh:
    def test_fresh_state(self, tmp_path):
        repo = tmp_path / "my-repo"
        _init_repo(repo)
        sha = _commit(repo, "initial commit")

        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "my-repo", str(repo), sha)

        result = KB_STALENESS.compute(vault)
        assert "my-repo" in result, "card not found in compute result"
        rec = result["my-repo"]

        assert rec["state"] == "fresh", (
            f"expected state=fresh, got {rec['state']!r}; full record: {rec}"
        )
        assert rec["drift_commits"] == 0, (
            f"expected drift_commits=0, got {rec['drift_commits']}"
        )
        assert rec["head"] == sha, (
            f"expected head={sha!r}, got {rec['head']!r}"
        )
        assert rec["documented_sha"] == sha


# ---------------------------------------------------------------------------
# 2. STALE — HEAD advanced recently (within STALE_DAYS)
# ---------------------------------------------------------------------------

class TestStale:
    def test_stale_state(self, tmp_path):
        repo = tmp_path / "stale-repo"
        _init_repo(repo)
        documented_sha = _commit(repo, "initial commit")
        # Advance HEAD: 1 new commit at "now" (no back-dating → age 0 days)
        new_head = _commit(repo, "advance HEAD", date_offset_days=0)

        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "stale-repo", str(repo), documented_sha)

        result = KB_STALENESS.compute(vault)
        rec = result["stale-repo"]

        # Must be SPECIFICALLY "stale" — not any other value
        assert rec["state"] == "stale", (
            f"expected state='stale', got {rec['state']!r}; full record: {rec}"
        )
        assert rec["drift_commits"] == 1, (
            f"expected drift_commits=1, got {rec['drift_commits']}"
        )
        assert rec["head"] == new_head
        assert rec["documented_sha"] == documented_sha
        assert rec["drift_age_days"] is not None and rec["drift_age_days"] <= KB_STALENESS.STALE_DAYS

    def test_stale_multiple_commits(self, tmp_path):
        """Multiple recent commits still yields stale (not very_stale)."""
        repo = tmp_path / "multi-repo"
        _init_repo(repo)
        documented_sha = _commit(repo, "base")
        _commit(repo, "c2", date_offset_days=0)
        new_head = _commit(repo, "c3", date_offset_days=0)

        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "multi-repo", str(repo), documented_sha)

        result = KB_STALENESS.compute(vault)
        rec = result["multi-repo"]

        assert rec["state"] == "stale", f"expected stale, got {rec!r}"
        assert rec["drift_commits"] == 2


# ---------------------------------------------------------------------------
# 3. VERY_STALE — drift oldest commit older than STALE_DAYS
# ---------------------------------------------------------------------------

class TestVeryStale:
    def test_very_stale_state(self, tmp_path):
        repo = tmp_path / "old-repo"
        _init_repo(repo)
        documented_sha = _commit(repo, "initial commit")
        # Back-date the drift commit well beyond STALE_DAYS (30 days ago)
        backdated_offset = -(KB_STALENESS.STALE_DAYS + 16)
        new_head = _commit(repo, "old undocumented change", date_offset_days=backdated_offset)

        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "old-repo", str(repo), documented_sha)

        result = KB_STALENESS.compute(vault)
        rec = result["old-repo"]

        assert rec["state"] == "very_stale", (
            f"expected state='very_stale', got {rec['state']!r}; full record: {rec}"
        )
        assert rec["drift_commits"] == 1
        assert rec["head"] == new_head
        assert rec["drift_age_days"] is not None and rec["drift_age_days"] > KB_STALENESS.STALE_DAYS

    def test_stale_vs_very_stale_boundary(self, tmp_path):
        """Demonstrate that banding flips correctly at the STALE_DAYS threshold.

        Three data points:
          - STALE_DAYS - 1 days ago → stale (well inside band)
          - exactly STALE_DAYS days ago → stale (inclusive <=)
          - STALE_DAYS + 2 days ago → very_stale (outside band)
        """
        # Stale side: back-date by only STALE_DAYS - 1 (still within band)
        repo_stale = tmp_path / "bnd-stale"
        _init_repo(repo_stale)
        sha_stale = _commit(repo_stale, "base")
        _commit(repo_stale, "recent drift", date_offset_days=-(KB_STALENESS.STALE_DAYS - 1))

        # Exact boundary: back-date by exactly STALE_DAYS → still stale (inclusive <=)
        repo_exact = tmp_path / "bnd-exact"
        _init_repo(repo_exact)
        sha_exact = _commit(repo_exact, "base")
        _commit(repo_exact, "boundary drift", date_offset_days=-KB_STALENESS.STALE_DAYS)

        # Very-stale side: back-date by STALE_DAYS + 2
        repo_very = tmp_path / "bnd-very"
        _init_repo(repo_very)
        sha_very = _commit(repo_very, "base")
        _commit(repo_very, "old drift", date_offset_days=-(KB_STALENESS.STALE_DAYS + 2))

        vault = tmp_path / "vault"
        _write_card(vault, "grp", "bnd-stale", str(repo_stale), sha_stale)
        _write_card(vault, "grp", "bnd-exact", str(repo_exact), sha_exact)
        _write_card(vault, "grp", "bnd-very", str(repo_very), sha_very)

        result = KB_STALENESS.compute(vault)
        assert result["bnd-stale"]["state"] == "stale", (
            f"expected stale side to be 'stale', got {result['bnd-stale']['state']!r}"
        )
        assert result["bnd-exact"]["state"] == "stale", (
            f"expected exact boundary (STALE_DAYS={KB_STALENESS.STALE_DAYS}) to be "
            f"'stale' (inclusive <=), got {result['bnd-exact']['state']!r}"
        )
        assert result["bnd-very"]["state"] == "very_stale", (
            f"expected very-stale side to be 'very_stale', got {result['bnd-very']['state']!r}"
        )


# ---------------------------------------------------------------------------
# 4. UNKNOWN variants
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_unknown_missing_path(self, tmp_path):
        """Card path points to a directory that does not exist."""
        vault = tmp_path / "vault"
        _write_card(
            vault, "1.0-dev", "ghost-repo",
            str(tmp_path / "nonexistent" / "ghost-repo"),
            "deadbeef" * 5,  # 40-char sha (doesn't matter — path won't exist)
        )

        result = KB_STALENESS.compute(vault)
        rec = result["ghost-repo"]
        assert rec["state"] == "unknown", (
            f"expected unknown for missing path, got {rec!r}"
        )
        # Must not raise; head should be None
        assert rec["head"] is None

    def test_unknown_not_a_git_repo(self, tmp_path):
        """Card path points to a plain directory (not a git repo)."""
        plain_dir = tmp_path / "not-git"
        plain_dir.mkdir()
        (plain_dir / "somefile.txt").write_text("hello", encoding="utf-8")

        vault = tmp_path / "vault"
        _write_card(vault, "1.0-dev", "not-git", str(plain_dir), "abc123" * 6 + "ab")

        result = KB_STALENESS.compute(vault)
        rec = result["not-git"]
        assert rec["state"] == "unknown", (
            f"expected unknown for non-git dir, got {rec!r}"
        )
        assert rec["head"] is None

    def test_unknown_sha_not_in_repo(self, tmp_path):
        """Card sha doesn't exist in the repo → unknown, but head is preserved.

        When the documented sha cannot be resolved, the engine already knows the
        repo's current HEAD.  Returning it (rather than None) lets /kb-status
        display the live HEAD alongside the "unknown" classification.
        """
        repo = tmp_path / "good-repo"
        _init_repo(repo)
        real_head = _commit(repo, "only commit")

        vault = tmp_path / "vault"
        _write_card(
            vault, "1.0-dev", "good-repo",
            str(repo),
            "0000000000000000000000000000000000000000",  # non-existent sha
        )

        result = KB_STALENESS.compute(vault)
        rec = result["good-repo"]
        assert rec["state"] == "unknown", (
            f"expected unknown for unrecognised sha, got {rec!r}"
        )
        # head is preserved so /kb-status can still display it.
        assert rec["head"] == real_head, (
            f"expected head={real_head!r} for unknown (sha-not-found), got {rec['head']!r}"
        )

    def test_no_exception_propagates(self, tmp_path):
        """compute() must never propagate exceptions — even for bad inputs."""
        vault = tmp_path / "vault"
        _write_card(vault, "x", "bad", "/does/not/exist/at/all", "badf00d" * 6)
        # Should not raise:
        result = KB_STALENESS.compute(vault)
        assert result["bad"]["state"] == "unknown"


# ---------------------------------------------------------------------------
# 5. NON-VACUITY PROOF
#
# Goal: demonstrate that the stale assertion in TestStale is NOT vacuous.
#
# Method: monkeypatch _run_git so that rev-list --count always returns 0,
# simulating a broken implementation that can never detect staleness.
# With this neuter applied, a stale repo's drift_commits is forced to 0.
# Fix #4 then checks head == resolved_sha; since HEAD *advanced* past the
# documented sha, the equality fails → state returns "unknown" (not "fresh"
# and certainly not "stale").
#
# We assert here that the neutered version does NOT return "stale".
# This proves: if the count comparison were vacuous, TestStale would go RED.
# ---------------------------------------------------------------------------

class TestNonVacuity:
    """Proves that TestStale's assertion is non-vacuous.

    The neuter: monkeypatch _run_git to return drift_commits=0 for rev-list
    --count calls, making every repo appear to have no drift.
    With the neuter in place and the fresh-detection equality guard (fix #4):
    - A stale repo (HEAD ahead of documented sha) has drift forced to 0.
    - head != resolved documented sha → state returns "unknown" (not "stale").
    - TestStale's assertion == "stale" would FAIL (RED).
    - This class asserts the neutered result is NOT "stale" to prove the guard is real.

    Note: the "drift" signal comes from `git rev-list --count doc..HEAD`.  To
    neutralise it we intercept rev-list calls and return count=0.  This accurately
    simulates a broken implementation that never reports staleness (analogous to
    always reading the `updated` frontmatter field that the generator rewrites
    every run).
    """

    def test_neuter_makes_stale_not_appear_stale(self, tmp_path, monkeypatch):
        """Neutering drift-count to 0 prevents the "stale" classification.

        This is the RED proof: TestStale's == "stale" assertion would fail under
        this neuter, demonstrating the staleness guard is non-vacuous.
        """
        repo = tmp_path / "neuter-repo"
        _init_repo(repo)
        documented_sha = _commit(repo, "base commit")
        _commit(repo, "advance HEAD")  # creates real drift

        vault = tmp_path / "vault"
        _write_card(vault, "grp", "neuter-repo", str(repo), documented_sha)

        # Capture the original _run_git for selective neuter.
        original_run_git = KB_STALENESS._run_git

        def _neutered_run_git(path: str, *args: str):
            # Neuter: when git asks how many commits are between documented..HEAD,
            # lie and say 0.  With fix #4's equality guard, this causes the
            # engine to detect head != documented_sha and return "unknown"
            # instead of "stale" — proving the guard is wired and non-vacuous.
            if (
                len(args) >= 3
                and args[0] == "rev-list"
                and "--count" in args
            ):
                return (0, "0")
            return original_run_git(path, *args)

        monkeypatch.setattr(KB_STALENESS, "_run_git", _neutered_run_git)

        result = KB_STALENESS.compute(vault)
        rec = result["neuter-repo"]

        # With neuter: drift_commits forced to 0, but head != documented_sha
        # (HEAD advanced), so the equality guard fires → "unknown", not "stale".
        # This PROVES TestStale's "stale" check would go RED under neuter.
        assert rec["state"] != "stale", (
            f"Neuter should suppress 'stale' classification (drift_count forced to 0 "
            f"+ head != resolved sha → unknown), got {rec['state']!r}."
        )

    def test_input_sensitivity(self, tmp_path):
        """Same repo, different documented_sha card → state flips fresh ↔ stale.

        This demonstrates the comparison is sensitive to input, ruling out vacuity.
        """
        repo = tmp_path / "sensitivity-repo"
        _init_repo(repo)
        first_sha = _commit(repo, "commit 1")
        head_sha = _commit(repo, "commit 2")  # HEAD advances past first_sha

        vault = tmp_path / "vault"

        # Card pointing at HEAD sha → fresh
        _write_card(vault, "grp", "at-head", str(repo), head_sha)
        # Card pointing at first sha → stale
        _write_card(vault, "grp", "at-c1", str(repo), first_sha)

        result = KB_STALENESS.compute(vault)

        assert result["at-head"]["state"] == "fresh", (
            f"expected fresh when sha==HEAD, got {result['at-head']['state']!r}"
        )
        assert result["at-c1"]["state"] == "stale", (
            f"expected stale when sha==first commit but HEAD is ahead, "
            f"got {result['at-c1']['state']!r}"
        )

    def test_outer_fence_bad_card_does_not_crash_good_card(self, tmp_path, monkeypatch):
        """Outer fence in compute(): one malformed card must not affect others.

        Non-vacuous proof: we monkeypatch _read_card_fields to raise an unexpected
        exception for the bad card only, then assert:
          - bad card  → state "unknown"  (outer fence caught it)
          - good card → state "fresh"    (other cards processed normally)

        This proves the outer try/except in compute() is genuinely load-bearing,
        not dead code — removing it would let the exception propagate and kill
        the good card's result.
        """
        # Build a real repo so the good card resolves to "fresh".
        repo = tmp_path / "good-repo"
        _init_repo(repo)
        sha = _commit(repo, "initial commit")

        vault = tmp_path / "vault"
        _write_card(vault, "grp", "bad-card", str(tmp_path / "irrelevant"), "aabbccdd" * 5)
        _write_card(vault, "grp", "good-card", str(repo), sha)

        original_read = KB_STALENESS._read_card_fields

        def _selective_raise(card: Path):
            if card.stem == "bad-card":
                raise RuntimeError("injected failure — outer fence test")
            return original_read(card)

        monkeypatch.setattr(KB_STALENESS, "_read_card_fields", _selective_raise)

        result = KB_STALENESS.compute(vault)

        assert result["bad-card"]["state"] == "unknown", (
            f"outer fence should yield unknown for bad card, got {result['bad-card']!r}"
        )
        assert result["good-card"]["state"] == "fresh", (
            f"good card must not be affected by bad card failure, "
            f"got {result['good-card']!r}"
        )


def test_relative_path_resolves_via_devroot(tmp_path, monkeypatch):
    import subprocess, importlib.util
    devroot = tmp_path / "root"; repo = devroot / "1.1-dev-tools" / "x"; repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    vault = tmp_path / "vault"; cards = vault / "02-projects" / "1.1-dev-tools"; cards.mkdir(parents=True)
    (cards / "x.md").write_text(f"---\npath: 1.1-dev-tools/x\nlast-documented-sha: \"{sha}\"\n---\n")
    monkeypatch.setenv("KB_DEV_ROOT", str(devroot))
    from pathlib import Path as _P
    spec = importlib.util.spec_from_file_location("kb_staleness", _P(__file__).resolve().parents[2] / "skills" / "kb-sync" / "kb-staleness.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    rec = m.compute(vault)
    assert rec["x"]["state"] == "fresh"
