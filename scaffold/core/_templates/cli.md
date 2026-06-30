<%*
const toolName = await tp.system.prompt("Tool name");
const cmdName = await tp.system.prompt("Command/subcommand name");
const projectCard = await tp.system.prompt("Project card title");
const aliasStr = await tp.system.prompt("Literal invocation alias (e.g. example-cli apply)");
-%>
---
type: cli
title: "<% tp.file.title %>"
aliases: ["<% aliasStr %>"]
tags: [type/cli]
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
up: "[[<% projectCard %>]]"
tool: "<% toolName %>"
command: "<% cmdName %>"
exit-code: 0
since: ""
---

# <% tp.file.title %>

> Atomic CLI command. Aliases hold the literal invocation string for wikilink resolution.

## Invocation

```bash
<% toolName %> <% cmdName %> [flags]
```

## Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| | | | |

## Exit codes

| Code | Meaning | Error note |
|---|---|---|
| 0 | Success | — |

## Examples

```bash
# Example
```

## Dev-loop role

<!-- When does an agent/operator call this? Reference the runbook or dev-loop doc. -->

## Related

- Project: [[<% projectCard %>]]
- Errors emitted: <!-- [[err-<tool>-N]] -->
- Runbooks: <!-- [[runbook-slug]] -->
