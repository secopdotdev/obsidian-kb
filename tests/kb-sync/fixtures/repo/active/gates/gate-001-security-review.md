---
type: gate
title: "PC1 — Security review gate"
gate-id: "fix-pc1"
status: open
blocking: true
gates: "prod-deploy"
requires: [external-audit]
created: 2026-06-14
tags: [type/gate]
criteria:
  - "External security review completed"
  - "All critical findings resolved"
  - "Sign-off from security lead"
---
# PC1 — Security review gate

- [ ] External security review completed
- [ ] All critical findings resolved
- [ ] Sign-off from security lead
