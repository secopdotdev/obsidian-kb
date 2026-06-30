# ============================================================
# Dockerfile — mission-control (embedded in knowledge-base repo)
# Multi-stage: node:22-alpine (build) → nginx-unprivileged (serve)
#
# BUILD CONTEXT: repo root (knowledge-base/).
# The app lives at web/ inside the repo; the vault IS the repo root.
# Run from knowledge-base/:
#   docker build -f web/Dockerfile -t mission-control:dev .
#
# The build stage COPYs the entire repo root into /vault (the vault),
# then COPYs web/ into /app (the Astro project). KB_VAULT=/vault wires
# them together — the content loader pattern !web/** in content.config.ts
# and 'web' in EXCLUDED_DIRS in astro.config.mjs ensure the app's own
# source tree inside /vault is never ingested as vault content.
#
# PREREQUISITE — graph.json:
# The dependency graph reads <vault>/00-meta/graph.json, a GITIGNORED
# rebuildable derivative. A clean `git clone` does NOT contain it, so the
# build context must be a working tree where kb-sync has already generated
# 00-meta/graph.json — otherwise the image builds fine but the homepage graph
# renders empty (graph.ts degrades silently). Generate it before docker build.
#
# The build stage asserts a non-trivial note count (see below) so a mis-wired
# vault fails the build loudly instead of shipping a blank site.
#
# DIGEST PINNING:
# Replace the @sha256:<PIN-AT-BUILD> placeholders below with the
# current digest before production deployment:
#   docker pull node:22-alpine && docker inspect node:22-alpine --format '{{index .RepoDigests 0}}'
#   docker pull nginxinc/nginx-unprivileged:1.27-alpine && docker inspect ...
# ============================================================

# ---- Stage 1: Build ----
# node:22-alpine — @sha256:<PIN-AT-BUILD>
FROM node:22-alpine AS build

WORKDIR /app

# Copy package files first for layer caching.
COPY web/package.json web/package-lock.json* ./

# Install dependencies (ci = reproducible from lockfile).
RUN npm ci --prefer-offline

# Copy the Astro source.
COPY web/ .

# Copy the entire repo root (vault) into /vault.
# The app's loaders exclude web/** so the web/ subtree inside /vault is harmless.
COPY . /vault/

# Wire the vault path via env var.
ENV KB_VAULT=/vault

# Prebuild (backlinks) + Astro build + Pagefind indexing.
RUN npm run build

# Fail the build loudly if the vault was not wired correctly (mis-set KB_VAULT,
# wrong build context, or the loaders ingested nothing). The live vault renders
# ~880 note pages; 500 is a safe floor that still trips on an empty ingest.
RUN count="$(find dist/notes -name index.html | wc -l)"; \
    echo "note pages built: $count"; \
    test "$count" -gt 500 || { echo "FATAL: vault ingest produced $count note pages (<500) — KB_VAULT/build-context misconfigured" >&2; exit 1; }

# ---- Stage 2: Serve ----
# nginxinc/nginx-unprivileged:1.27-alpine — @sha256:<PIN-AT-BUILD>
# Non-root by base image design (UID 101, port 8080).
FROM nginxinc/nginx-unprivileged:1.27-alpine AS serve

# Remove the default nginx config; replace with our custom one.
COPY web/nginx.conf /etc/nginx/conf.d/default.conf

# Copy static build output.
COPY --from=build /app/dist /usr/share/nginx/html

# Pagefind index is built into dist/ — nothing extra needed.

EXPOSE 8080

# Container-level health signal (busybox wget ships in the nginx-unprivileged base).
# K8s probes in k8s/deployment.yaml override this; it covers bare `docker run` +
# a docker-compose deployment.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost:8080/ >/dev/null 2>&1 || exit 1

# nginx-unprivileged already runs as UID 101 (nginx user).
# readOnlyRootFilesystem is enforced in k8s/deployment.yaml via emptyDirs.
