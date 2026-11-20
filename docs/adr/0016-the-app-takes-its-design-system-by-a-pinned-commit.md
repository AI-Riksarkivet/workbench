# 0016. The app takes its design system by a pinned commit (2026-11-18)

**Status:** accepted. Source: decided with the owner on 2026-11-18 with the UI repository's shape: read from source,
consumed by pinning a commit, no release, tag or registry. The owner's exact words on the pin are not recorded. Landed in
`8f7ab41`.

## Context

The app reads the design system from source, with no build step, and one person changes both repositories.

## Decision

- `frontend/app/package.json` names `github:AI-Riksarkivet/ui#<full commit sha>`; `bun.lock` records it.
- Upgrading is bumping the sha and running `bun --cwd=frontend install`, after reading the commits in between.
- The app owns the one copy of each peer (`svelte`, `bits-ui`, `tailwindcss`, `vite`, `gsap`).

## Consequences

- No change in the UI repository reaches the app until the pin moves, so a pin bump is a reviewed change of its own.
- The image's frozen install fetches that commit from GitHub.
