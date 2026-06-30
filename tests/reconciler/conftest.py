import json, pytest
from pathlib import Path
from reconciler_helpers import make_project  # noqa: F401 — re-exported for fixture injection

CARD_TMPL = """---
type: project
title: "{name}"
aliases: []
tags: [type/project, "group/{group}", "tool/{name}"]
group: "{group}"
repo: "{repo}"
path: {path}
last-documented-sha: "{sha}"
---
# {name}
"""

@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "kb"
    (v / "02-projects").mkdir(parents=True)
    (v / "00-meta/scout-cache").mkdir(parents=True)
    (v / "04-cli-errors").mkdir(parents=True)
    (v / "reconcile").mkdir()
    (v / "00-meta/kb-manifest.json").write_text("[]", encoding="utf-8")
    (v / "00-meta/project-edges.yaml").write_text("# edges\n", encoding="utf-8")
    (v / "00-meta/retired-projects.txt").write_text("# prune allowlist\n", encoding="utf-8")
    return v
