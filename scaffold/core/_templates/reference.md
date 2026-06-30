<%*
const refUrl = await tp.system.prompt("Canonical upstream URL");
-%>
---
type: reference
title: "<% tp.file.title %>"
aliases: []
tags: [type/reference]
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
url: "<% refUrl %>"
---

# <% tp.file.title %>

> One-sentence description: what this resource covers and why it is referenced here.

## Why referenced

<!-- Decision or gap this resource addresses. One paragraph maximum. -->

## Key facts

<!-- Lookup table, decision criteria, or summary bullets. Prefer tables. -->

| Aspect | Detail |
|---|---|
| | |

## Source

[<% tp.file.title %>](<% refUrl %>)

## Related internal notes

<!-- Notes in this vault that depend on or link to this reference. -->

- <!-- [[note]] -->
