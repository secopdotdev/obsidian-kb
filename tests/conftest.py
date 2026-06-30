"""Root conftest.py — shared path wiring for all test suites.

Adds scaffold/core to sys.path so that ``import tools.reconciler.*`` resolves
from the repo layout (scaffold/core/tools/reconciler/) without installing anything.

Also adds tests/reconciler/ to sys.path so that ``from reconciler_helpers import
make_project`` resolves from any reconciler test module.
"""
import sys
from pathlib import Path

# Repo root is two levels up from this file (tests/ -> repo root).
_TESTS_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_ROOT.parent
_SCAFFOLD_CORE = _REPO_ROOT / "scaffold" / "core"
_RECONCILER_TESTS = _TESTS_ROOT / "reconciler"

for _p in (_SCAFFOLD_CORE, _RECONCILER_TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
