# 0007. Every canvas renders with WebGPU only (2026-11-14)

**Status:** accepted. Source: the owner's rule of 2026-11-14 (issue #10): a workbench canvas renders with WebGPU;
Annotorious is not an option, and neither is any library that draws only with WebGL or Canvas 2D.

## Context

The annotator's canvas ran pixi.js with a string preference for WebGPU, which falls back to WebGL and then Canvas. The
explorer's atlas and graph were already hand-written WebGPU.

## Decision

- pixi runs with `preference: ['webgpu']` (an array: no fallback), under the strict CSP through `pixi.js/unsafe-eval`.
- The atlas, the graph and the time strip are hand-written WebGPU in `#lib/gpu`.
- Where a browser has no WebGPU, each canvas shows the plain "WebGPU is required" state and fetches nothing to draw.
- A rendering library is a candidate only if it actually renders with WebGPU, read from its source.

## Consequences

- Browsers without WebGPU cannot label or see the atlas or graph: Firefox on Linux by default, and virtual desktops
  without a GPU often refuse an adapter. Which browsers the users run was asked on 2026-11-14 and is not recorded.
- The pixi version range in `frontend/app/package.json` (`^8.19.0`) admits 8.20, which changes WebGPU shader overrides;
  the lock holds 8.19.0.
