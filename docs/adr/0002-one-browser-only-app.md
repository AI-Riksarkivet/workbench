# 0002. One browser-only app, no micro-frontends (2026-11-14)

**Status:** accepted. Source: the owner, 2026-11-14, "No microfrontend" (issue #9, body). The browser-only shape
(adapter-static, `ssr = false`, no server code, the strict CSP sent as a header) is the target shape #9 sets out; the
owner's recorded words are the ones above, and the delivery steps that built it are #9's comments from 2026-11-14 on.

## Context

At the move the frontend was three SvelteKit 2 apps (explorer, annotator, studio), each rendering on a server, each on
its own path under rask's old gateway, each turning rask's session cookie into a bearer. They read secrets from the
environment and could not be deployed.

## Decision

- The workbench's UI is one SvelteKit single-page app, `frontend/app`, served as static files by its own `serve.ts`
  with the strict CSP as a header.
- It has no server code: it signs in with PKCE in the browser, keeps tokens in memory, and calls each service
  same-origin with the person's token.
- A dataset is the place and every tool is a view of it; exploring and labeling live in one route tree.

## Consequences

- One image and one route at `/`.
- What the old servers did beyond attaching a token (media range requests, Arrow re-encoding, the model-ingress
  allowlist) moved into the services.
- The old apps stay in the tree until each is deleted, never deployable (`frontend/annotator`, `frontend/explorer`'s
  `/workflow`, `frontend/studio`).
