---
title: "Secrets management approach"
status: accepted
date-decided: 2026-05-01
---

# Secrets management approach

This ADR records the decision on how to store and access secrets in the project.

## Considered options

### OpenBao

- Pros: self-host | audited, open source, supports dynamic secrets
- Cons: ops burden, requires dedicated server
- Cost: med

### DPAPI

- Pros: zero-dep, built into Windows
- Cons: Windows-only, not portable to Linux containers
- Cost: low

## Decision

Recommendation: OpenBao

OpenBao provides auditable, cross-platform secrets management with support for dynamic credentials.
