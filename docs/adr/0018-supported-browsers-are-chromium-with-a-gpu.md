# 0018. The supported browsers are current Chrome and Edge on a machine with a GPU (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, to "state current Chrome or Edge on a
machine with a GPU as the supported target and show a clear page otherwise, with no fallback renderer": "for now yes?".

## Context

Rendering is WebGPU only (ADR 0007). That excludes Firefox on Linux by default, older Safari and remote desktops without
a graphics card. The design system's theme CSS needs relative colour syntax (Chromium 119, Firefox 128, Safari 16.4).
The browsers a production install must serve were asked on 2026-11-14 and not answered until now.

## Decision

- The supported target is current Chrome or Edge on a machine with a GPU.
- A browser that cannot run the app is told so on a page of its own. There is no fallback renderer.

## Consequences

- The browser suite runs Chromium only, and that is the whole supported surface.
- "For now": a production target with other browsers reopens this decision, and ADR 0007 with it.
