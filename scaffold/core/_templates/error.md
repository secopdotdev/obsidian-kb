<%*
const toolName = await tp.system.prompt("Tool name");
const exitCode = await tp.system.prompt("Exit code integer");
const errMsg = await tp.system.prompt("Exact error message string");
const projectCard = await tp.system.prompt("Project card title");
-%>
---
type: error
title: "<% tp.file.title %>"
aliases: ["exit-<% exitCode %>", "<% errMsg %>"]
tags: [type/error]
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
up: "[[<% projectCard %>]]"
tool: "<% toolName %>"
code: "<% exitCode %>"
exit-code: <% exitCode %>
since: ""
---

# <% tp.file.title %>

> Atomic error/exit-code note. Aliases hold the exact message string and `exit-N` code for wikilink resolution.

## Error message

```
<% errMsg %>
```

**Exit code:** `<% exitCode %>`

## Trigger conditions

<!-- What state or input causes this code to be emitted. Be precise. -->

## Fix

1. <!-- Step 1 -->
2. <!-- Step 2 -->

## Emitted by

<!-- [[cmd-<tool>-<command>]] — the CLI note that surfaces this error. -->

## Related

- Project: [[<% projectCard %>]]
- Command: <!-- [[cmd-<% toolName %>-<command>]] -->
- ADRs: <!-- [[<project>-adr-NNNN-...]] -->
