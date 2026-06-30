---
type: moc
title: "_INDEX"
aliases: ["system map", "all tiers"]
tags: [type/moc]
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
---

# System Development Map

> All-tiers operator overview — the local development map. Entry point for Breadcrumbs hierarchy and agent cold-start. Hyphenated keys via `file.frontmatter["..."]`.

## Tiers

| Tier | MOC | Domain |
|---|---|---|
| 01-secops | [[01-secops]] | Security operations — Sentinel, Defender XDR, CrowdStrike, SOAR, detection engineering |
| 02-platform | [[02-platform]] | Platform infrastructure — K3s/Talos, Docker, IaC, homelab, reverse proxies |
| 03-apps | [[03-apps]] | Applications, agents, and developer tooling (devkit) |

## All projects

```dataview
TABLE WITHOUT ID file.link AS Project, tier AS Tier, status AS Status, file.frontmatter["rag-flag"] AS Flag, file.frontmatter["next-action"] AS Next
FROM "02-projects"
WHERE type = "project"
SORT file.frontmatter["rag-flag"] ASC, tier ASC
```

## Red / yellow flags (all tiers)

```dataview
TABLE WITHOUT ID file.link AS Project, tier AS Tier, file.frontmatter["rag-flag"] AS Flag, file.frontmatter["blockers"] AS Blockers
FROM "02-projects"
WHERE type = "project" AND file.frontmatter["rag-flag"] != "green"
SORT tier ASC
```

## Open ADRs (all tiers)

```dataview
TABLE WITHOUT ID file.link AS ADR, project AS Project, tier AS Tier, status AS Status
FROM "03-adr"
WHERE type = "adr" AND status = "proposed"
SORT file.name ASC
```

## Stale docs

```dataview
LIST FROM "02-projects"
WHERE type = "project" AND stale = true
SORT tier ASC
```
