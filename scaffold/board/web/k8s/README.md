# k8s/ — SUPERSEDED

> **Operator decision (2026-06-19):** the Kubernetes deploy path is **cancelled**.
> The board runs on the local network with minimal overhead via `astro preview --host`.
> These manifests are retained for reference only. Do NOT apply them.

---

## What was planned

These manifests described a hardened k3s/k3d deployment:

- `namespace.yaml` — restricted PodSecurity namespace `mission-control`
- `deployment.yaml` — nginx-unprivileged, non-root, readOnly rootfs, emptyDir tmp
- `service.yaml` — ClusterIP :80 → :8080
- `middleware.yaml` — Traefik CSP + security headers
- `ingressroute.yaml` — Traefik HTTPS IngressRoute

## What replaced it

Static build + `astro preview --host` on the host machine. See the
"Run on the local network" section in [../README.md](../README.md).

No Docker, no container orchestration, no ingress controller. The Pagefind index
is included in `dist/pagefind/` after `npm run build`, so search works with preview.
