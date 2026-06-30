---
type: moc
title: "<% tp.file.title %>"
aliases: []
tags: [type/moc, "tier/<% tp.file.title %>"]
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
up: "[[_INDEX]]"
---

# <% tp.file.title %>

> Tier MOC — exec summary of this tier's domain and projects. <!-- one sentence -->

## Projects (this tier)

```dataview
TABLE WITHOUT ID file.link AS Project, status AS Status, file.frontmatter["rag-flag"] AS Flag, file.frontmatter["next-action"] AS Next
FROM "02-projects"
WHERE type = "project" AND tier = this.file.name
SORT file.frontmatter["rag-flag"] ASC, file.name ASC
```

## Red / yellow flags (this tier)

```dataview
TABLE WITHOUT ID file.link AS Project, file.frontmatter["rag-flag"] AS Flag, file.frontmatter["blockers"] AS Blockers
FROM "02-projects"
WHERE type = "project" AND tier = this.file.name AND file.frontmatter["rag-flag"] != "green"
SORT file.name ASC
```

## Open ADRs (this tier)

```dataview
TABLE WITHOUT ID file.link AS ADR, project AS Project, status AS Status
FROM "03-adr"
WHERE type = "adr" AND tier = this.file.name AND status = "proposed"
SORT file.name ASC
```

---
Up: [[_INDEX]]
