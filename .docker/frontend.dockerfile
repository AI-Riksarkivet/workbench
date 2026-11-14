# syntax=docker/dockerfile:1.11-labs
# The workbench app image: the one browser-only single-page app (SvelteKit, adapter-static), built with
# Bun and served by its own static server, `frontend/app/serve.ts`. Build context = repo root.
#
# The image holds the built files and the server, nothing else: no node_modules (serve.ts uses only Bun
# and node:path), no credential, no environment. The release mounts `/config.json` (the issuer, client,
# scopes and the services' public bases) and names it with WORKBENCH_APP_CONFIG_FILE.
#
# Built by Dagger (`dagger call image --name=frontend`), never docker.

# ---- builder: bun install (workspace) + build the app ------------------------------
# The bun the workspace's lock was written by (frontend/package.json `packageManager`, .dagger's BunImage).
# Of this stage only the build, the bun binary and tini-static ship.
FROM oven/bun:1.4.2-debian@sha256:4f6e31d1a54d6a3dd312daef655fc998101b5043d52e12592ac293ef04b9bc73 AS builder

# tini-static for the runtime stage, which has no package manager to install it with.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends tini

WORKDIR /src

# LAYER ORDER IS THE COST MODEL OF THIS FILE. `bun install --frozen-lockfile` needs every workspace member
# PRESENT, but only its package.json: manifests first, install, sources after, so a source edit re-runs
# only the build. `--parents` (the -labs frontend) and the `/./` pivot re-root each manifest under /src,
# the workspace root, so bun's globs resolve unchanged and a new member is picked up with no edit here.
COPY frontend/package.json frontend/bun.lock ./
COPY frontend/.oxlintrc.json frontend/.oxfmtrc.json ./
# patchedDependencies: bun install --frozen-lockfile resolves these relative to the workspace root.
COPY frontend/patches patches
COPY --parents frontend/./*/package.json frontend/./packages/*/package.json /src/

# `--ignore-scripts`: a member's `prepare` (`svelte-kit sync`) needs its sources, which are not here yet;
# `vite build` syncs the app itself. Patches are applied by the install, not by a script.
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile --ignore-scripts

# Sources AFTER the install. COPY overlays rather than replaces, so the node_modules symlink trees bun
# created inside each member survive (.dockerignore strips node_modules and .svelte-kit).
COPY frontend/app app
COPY frontend/packages packages

# The app compiles @workbench/ui from its source (its exports name `src/`), so no package builds first.
# hadolint ignore=DL3059
RUN bun run --cwd=app build

# ---- final: the bun binary on distroless, serving the built files -----------------
# Distroless rather than an oven/bun runtime tag: the server needs only bun and glibc, and distroless/base-nossl is
# glibc, CA certificates and tzdata with no shell, package manager or OpenSSL, rebuilt as Debian publishes fixes.
# hadolint ignore=DL3026  # Reason: gcr.io/distroless is Google's official distroless registry; digest-pinned.
FROM gcr.io/distroless/base-nossl-debian13@sha256:d710a5c8bfe4cf7e726d7e1afd73e363a9fb02c2a11ad893d2d6185c1b9a89cb

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/workbench" \
      org.opencontainers.image.title="workbench-app" \
      org.opencontainers.image.description="The workbench: the single-page app and its static server"

# tini as PID 1, so SIGTERM reaches the server and a rollout drains it.
COPY --from=builder /usr/bin/tini-static /usr/bin/tini
COPY --from=builder /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /app

# Read-only at runtime: the server reads these and writes nothing, so a read-only root filesystem holds.
COPY --from=builder --chown=0:0 /src/app/build ./build
COPY --from=builder --chown=0:0 /src/app/serve.ts ./serve.ts

# A numeric non-root uid, so the kubelet can verify `runAsNonRoot`; it needs no passwd entry, and HOME points at
# the release's /tmp mount.
USER 10001
# BUN_RUNTIME_TRANSPILER_CACHE_PATH=0 keeps bun from writing a transpiler cache, which a read-only root refuses.
ENV PORT=3000 \
    HOME=/tmp \
    BUN_RUNTIME_TRANSPILER_CACHE_PATH=0
EXPOSE 3000

# Exec form: the image has no shell.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["bun", "-e", "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

ENTRYPOINT ["tini", "--"]
CMD ["bun", "serve.ts"]
