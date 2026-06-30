# Mission Control board

The Astro static site embedded in the `knowledge-base` repo at
`knowledge-base/web/`. Human-readable, searchable view of the KB vault — the vault root
is the repo root one level up (`..`).

Stack: **Astro 6.4.x** (Content Layer API, Zod 4) · **Pagefind 1.5.x** · served via `astro preview --host` on the local network.

---

## Vault dependency

This app is embedded in the `knowledge-base` repo at `knowledge-base/web/`. The vault root is exactly one level up (`..` from `web/`). No submodule or sibling directory is needed — just run from `knowledge-base/web/` and the default path resolves correctly.

Override the vault path with `KB_VAULT` (Docker build sets `KB_VAULT=/vault`).

The build **succeeds** with an absent vault (produces an empty site with no notes). All vault-reading code degrades gracefully.

---

## Scanner-ignore requirement

The `web/` subtree must never be harvested as vault content. Two guards enforce this within the app itself:

- `astro.config.mjs` — `EXCLUDED_DIRS` includes `'web'`, so the wikilink-resolution walk skips `web/` (and `web/node_modules/**`) when scanning the vault.
- `src/content.config.ts` — the content loader `pattern` includes `'!web/**'`, so Astro never ingests the app's own Markdown as notes.

The kb-sync scanners (in the `~/.claude/skills/kb-sync` skill, outside this repo) must also be configured to ignore the `web/` subtree so the app's source is never harvested as vault content. Confirm the kb-sync harvest exclude list covers `web/` before running a full sync after this merge.

---

## Local development

```bash
npm install
node scripts/build-backlinks.mjs   # generates src/data/backlinks.json
npm run dev                         # astro dev on :4321 (localhost only)
```

> Note: `astro dev` binds IPv6 localhost only (`[::1]:4321`). `http://localhost:4321`
> works but `http://127.0.0.1:4321` and other LAN devices will NOT reach it.
> Use `npm run serve` (see below) for LAN access, or `npm run dev:lan` for a dev-mode LAN bind.

> Note: Pagefind search does not work in `dev` mode. Run `npm run build` and `npm run serve` to test search on the LAN.

---

## Run on the local network

`astro preview --host` serves the production build on **all interfaces** (0.0.0.0 + ::),
making the board reachable from any device on the LAN — no Docker, no k8s, no extra deps.

```powershell
cd <your-vault>\web
npm run build   # prebuild → astro build → pagefind index → dist/
npm run serve   # astro preview --host --port 4321 — binds all interfaces
```

To find your host's LAN IP (Windows):

```powershell
ipconfig | Select-String "IPv4"
# Look for the 192.168.x.x or 10.x.x.x address on your LAN adapter
```

Access the board from any LAN device at:

```
http://<lan-ip>:4321/
```

Example: `http://192.168.1.42:4321/`

**Why not `npm run dev`?** The Astro dev server binds IPv6 localhost only (`[::1]:4321`),
so `http://127.0.0.1:4321` fails and no other LAN device can reach it. `astro preview --host`
binds all interfaces and serves the fully-built static output including the Pagefind search index.

---

## Build

```bash
npm run build
# Runs: prebuild (build-backlinks.mjs) → astro build → npx pagefind --site dist
```

Output: `dist/` (static HTML + `dist/pagefind/` Pagefind index).

---

## Docker build

The Dockerfile build context is the repo root (`knowledge-base/`). The app is at `web/` inside the repo; the vault IS the repo root. Run from `knowledge-base/`:

```bash
docker build -f web/Dockerfile -t mission-control:dev .
```

**Pin image digests before production deploy** — see comments in `Dockerfile`.

---

## Kubernetes deploy (local k3d validation) — SUPERSEDED

> **SUPERSEDED (2026-06-19):** The Kubernetes deploy path is **cancelled** per operator
> decision. The board runs on the local network via `npm run serve` (see "Run on the
> local network" above). The manifests in `k8s/` are retained for reference only — see
> [`k8s/README.md`](k8s/README.md). The steps below are preserved for historical context.

## Kubernetes deploy (historical reference)

### Pre-requisites

- k3d + kubectl + helm
- Docker with image `mission-control:dev` built

### 6-gate local validation

```bash
# Gate 0 — Create ephemeral k3d cluster (Traefik included in k3s)
k3d cluster create kb-dev \
  --port "80:80@loadbalancer" \
  --port "443:443@loadbalancer"

# Build and import the image
docker build -f web/Dockerfile -t mission-control:dev .
k3d image import mission-control:dev -c kb-dev

# Detect Traefik version and set correct CRD apiVersion
kubectl get crd ingressroutes.traefik.io > /dev/null 2>&1 && \
  echo "Traefik v3 — apiVersion: traefik.io/v1alpha1 (default, no change needed)" || \
  echo "Traefik v2 — run: sed -i 's|traefik.io/v1alpha1|traefik.containo.us/v1alpha1|g' k8s/middleware.yaml k8s/ingressroute.yaml"

# Apply manifests
kubectl apply -f web/k8s/

# Add local hostname resolution (macOS/Linux: /etc/hosts, Windows: C:\Windows\System32\drivers\etc\hosts)
# 127.0.0.1  kb.local

# --- Gate 1: Site returns 200 ---
curl -k --resolve kb.local:443:127.0.0.1 https://kb.local/ -o /dev/null -w "%{http_code}"
# Expected: 200

# --- Gate 2: A known note renders ---
# Replace with an actual note slug from the build
curl -k --resolve kb.local:443:127.0.0.1 https://kb.local/notes/Home -o /dev/null -w "%{http_code}"
# Expected: 200

# --- Gate 3: Pagefind search returns a hit ---
# Navigate to https://kb.local/search in a browser and type a known keyword.

# --- Gate 4: Security headers present ---
curl -k --resolve kb.local:443:127.0.0.1 https://kb.local/ -sv 2>&1 | \
  grep -iE "strict-transport|content-security|x-frame|x-content-type"
# Expected: all four headers present

# --- Gate 5: No CSP console violations ---
# Open https://kb.local/ in Chrome DevTools → Console.
# Check for "Refused to..." CSP errors.
# Pagefind WASM may need wasm-unsafe-eval (already included in middleware.yaml).
# If wasm-unsafe-eval is confirmed NOT needed, remove it from middleware.yaml and redeploy.

# --- Gate 6: Container runs non-root read-only ---
kubectl exec -n mission-control deploy/mission-control -- id
# Expected: uid=101(nginx) gid=101(nginx)
kubectl exec -n mission-control deploy/mission-control -- touch /test-write 2>&1
# Expected: "Read-only file system" error (confirms readOnlyRootFilesystem)

# --- Cleanup ---
k3d cluster delete kb-dev
```

---

## Gate 2 — Live deploy (PAUSE)

After all 6 local validation gates pass:

1. Build and push the image to your registry with a digest pin.
2. Update `k8s/deployment.yaml` `image:` to `<registry>/mission-control:<tag>@sha256:<digest>`.
3. Update `k8s/ingressroute.yaml` `Host()` match to your actual domain.
4. Supply your kubeconfig/context and apply: `kubectl apply -f k8s/`.

**Do NOT apply to a live cluster autonomously** — operator review required per Gate 2 in the plan.

---

## Traefik CRD version

k3s commonly ships Traefik 2 (`traefik.containo.us/v1alpha1`) or Traefik 3 (`traefik.io/v1alpha1`).

```bash
# Detect which version your cluster has:
kubectl get crd ingressroutes.traefik.io > /dev/null 2>&1 && echo "v3" || echo "v2 (check traefik.containo.us)"

# Switch manifests to v2 if needed:
sed -i 's|traefik.io/v1alpha1|traefik.containo.us/v1alpha1|g' k8s/middleware.yaml k8s/ingressroute.yaml
```

---

## File layout

```
web/
├── src/
│   ├── content.config.ts          # Content Layer API — glob loader + Zod 4 schema
│   ├── pages/
│   │   ├── index.astro            # Landing — RAG dashboard from kb-manifest.json
│   │   ├── notes/[...id].astro    # Per-note render
│   │   ├── tiers/[tier].astro     # Per-tier project list
│   │   └── search.astro           # Pagefind search UI
│   ├── layouts/
│   │   ├── Base.astro             # Outer HTML shell
│   │   └── Note.astro             # Note layout (TOC, chips, backlinks rail)
│   ├── components/
│   │   ├── FlagBadge.astro        # 🔴🟡🟢 RAG badge
│   │   ├── Breadcrumb.astro       # Breadcrumb trail
│   │   ├── Backlinks.astro        # "What links here" panel
│   │   ├── FrontmatterChips.astro # Frontmatter chip row
│   │   └── TierNav.astro          # Sidebar tier navigation
│   ├── data/
│   │   └── backlinks.json         # GENERATED by prebuild; stub {} committed
│   └── styles/
│       └── global.css             # Full design system
├── scripts/
│   └── build-backlinks.mjs        # Prebuild: parse wikilinks → backlinks.json
├── public/
│   └── favicon.svg
├── astro.config.mjs               # Astro 6 config + remark-wiki-link
├── package.json
├── Dockerfile                     # node:22-alpine → nginx-unprivileged
├── nginx.conf                     # try_files, immutable cache, no security headers
├── k8s/
│   ├── namespace.yaml             # ns mission-control, restricted PodSecurity
│   ├── deployment.yaml            # Hardened: non-root, readOnly, drop ALL, emptyDirs
│   ├── service.yaml               # ClusterIP port 80 → 8080
│   ├── middleware.yaml            # Traefik securityHeaders + CSP
│   └── ingressroute.yaml          # Traefik IngressRoute (HTTPS)
├── .dockerignore
└── .gitignore
```
