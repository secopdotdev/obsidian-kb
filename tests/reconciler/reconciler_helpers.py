"""Shared helper utilities for the reconciler test suite.

Kept separate from conftest.py so test modules can import make_project directly
without relying on conftest module resolution order.
"""
import json
from pathlib import Path

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


def make_project(v, name, group, path=None, repo="git@github.com:example-org/%s.git", sha="abc1234"):
    path = path or f"{group}/{name}"
    card = v / "02-projects" / group / f"{name}.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(CARD_TMPL.format(name=name, group=group, path=path, repo=repo % name, sha=sha), encoding="utf-8")
    (v / "00-meta/scout-cache" / f"{name}.json").write_text(json.dumps({"identity": {"name": name}}), encoding="utf-8")
    man = json.loads((v / "00-meta/kb-manifest.json").read_text())
    man.append({"name": name, "group": group, "last_documented_sha": sha, "card_path": f"02-projects/{group}/{name}.md"})
    (v / "00-meta/kb-manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    return card
