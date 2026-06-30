---
type: reference
title: tag-taxonomy
aliases: []
tags: [type/reference]
status: active
created: 2026-06-11
updated: 2026-06-11
related: ["[[conventions]]"]
---

# tag-taxonomy

Controlled tag vocabulary for the knowledge base. Tags are namespaced with `/` separators. Only tags from this list should appear in note frontmatter.

---

## Tag namespaces

### tier/ — which system tier owns this note

| Tag | Meaning |
|---|---|
| `tier/01-secops` | Security operations tier (Sentinel, Defender XDR, CrowdStrike, SOAR) |
| `tier/02-platform` | Platform / infrastructure tier (K3s, K8s, vCenter, Traefik, IaC) |
| `tier/03-apps` | Application tier (first-party tooling, bots, pipelines) |

### type/ — note type (mirrors `type:` frontmatter property)

| Tag | Meaning |
|---|---|
| `type/project` | Project hub card |
| `type/adr` | Architecture Decision Record stub |
| `type/cli` | Atomic CLI command note |
| `type/error` | Atomic error/exit-code note |
| `type/runbook` | Operational procedure |
| `type/reference` | Reference material, catalog, or external doc |
| `type/moc` | Map of Content / index note |

### domain/ — technical domain (cross-tier)

| Tag | Meaning |
|---|---|
| `domain/azure` | Microsoft Azure services |
| `domain/sentinel` | Microsoft Sentinel (SIEM/SOAR) |
| `domain/defender` | Microsoft Defender XDR |
| `domain/crowdstrike` | CrowdStrike Falcon platform |
| `domain/k8s` | Kubernetes / K3s |
| `domain/git` | Git tooling and workflows |
| `domain/security` | Security-specific patterns (not tied to one product) |
| `domain/python` | Python language / ecosystem |
| `domain/go` | Go language / ecosystem |

### status/ — lifecycle state (use only when status property alone is not enough for filtering)

| Tag | Meaning |
|---|---|
| `status/active` | In active use |
| `status/deprecated` | No longer recommended; kept for history |

### flag/ — RAG (Red/Amber/Green) attention signal

| Tag | Meaning |
|---|---|
| `flag/red` | Needs immediate attention; blocker present |
| `flag/yellow` | Watch item; degraded or at risk |
| `flag/green` | Healthy; no action required |

---

## When to use tag vs wikilink vs property

| Signal | Use |
|---|---|
| "This note belongs to tier X" | `tier/` tag **and** a `[[tier-moc]]` wikilink in body |
| "This note is of type Y" | `type: Y` property (frontmatter) + `type/Y` tag |
| "This note is related to another note" | `related: ["[[note]]"]` property + inline `[[wikilink]]` at point of mention |
| "This note has a lifecycle state" | `status: <value>` property; optionally `status/` tag if you need tag-based filtering in Bases |
| "This note needs human attention" | `rag-flag: red|yellow|green` property on project cards; `flag/` tag on any note type |
| "This note is in a specific domain" | `domain/` tag — no property equivalent |

**Rule of thumb:** properties are the machine-readable API (Bases/Dataview queries, agent parsing). Tags are the human-browsable facets (tag pane, sidebar). Wikilinks build the graph (backlinks, path-finding). Never duplicate the same fact across all three; use the right layer for the right purpose.
