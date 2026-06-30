<%*
const adrStatus = await tp.system.suggester(["proposed","accepted","rejected","superseded"], ["proposed","accepted","rejected","superseded"], false, "ADR status");
const adrTier = await tp.system.suggester(["01-secops","02-platform","03-apps"], ["01-secops","02-platform","03-apps"], false, "Tier");
const adrId = await tp.system.prompt("ADR ID (4-digit, e.g. 0001)");
const projectName = await tp.system.prompt("Project name (matches project card title)");
const adrSource = await tp.system.prompt("Repo ADR path (e.g. active/decisions/0001-slug.md)");
-%>
---
type: adr
title: "<% tp.file.title %>"
aliases: []
tags: [type/adr, "tier/<% adrTier %>", "project/<% projectName %>"]
status: "<% adrStatus %>"
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
related: []
adr-id: "<% adrId %>"
project: "<% projectName %>"
tier: "<% adrTier %>"
supersedes: ""
superseded-by: ""
deciders: []
date-decided: ""
source: "<% adrSource %>"
---

# <% tp.file.title %>

> One-line decision statement: what was decided.

**Project:** [[<% projectName %>]] | **Status:** `<% adrStatus %>` | **ID:** `<% adrId %>`

## Context

<!-- Why this decision was needed; alternatives considered. Keep to 2–3 sentences. -->

## Decision

<!-- The choice made and the primary rationale. -->

## Supersede chain

<!-- If supersedes an earlier ADR: [[<superseded-adr>]]. If superseded by a later one: [[<superseding-adr>]]. -->

## Full body

[ADR body in repo →](<% adrSource %>)

> [!warning] Stub only
> This card holds metadata. The authoritative decision body lives in the repo at `source`.
