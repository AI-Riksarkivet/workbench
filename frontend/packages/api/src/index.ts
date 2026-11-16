// @workbench/api — shared frontend data layer, split by domain. JIT TS: apps import the source directly
// (Vite/svelte-check transpile it) — no build. The `.` entry is CLIENT-SAFE (no node:crypto / $env):
//   • edge     — the cluster edge's base (`WORKBENCH_EDGE_URL`) + the SSR `/api/*` → edge rewrite (handleFetch factory).
//   • parse    — the valibot parse-don't-validate boundary for typed client responses.
//   • me       — the frozen `GET /management/v1/me` identity contract (schema/types) + the BFF-side fetchMe helper.
// Server-only auth lives behind subpaths so it never reaches a client bundle:
//   • @workbench/api/oidc — the OIDC crypto seam (PKCE, sealed AES-256-GCM session cookie).
//   • @workbench/api/bff  — the SvelteKit BFF factories (makeOidcConfig / makeSessionHandle / makeBackendProxy).
export * from './edge';
export * from './parse';
export * from './me';
// The compute client: `ray` reads the Ray cluster's jobs, nodes and Serve applications through the `ray` service.
export * from './ray';
