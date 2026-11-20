# 0015. The design system lives in AI-Riksarkivet/ui, and Svelte Bits is never copied into it (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18, through the question tool: a new repository for the design system
(its name "ui", "Public"); rask's console adopts it "after it proves out" in the workbench, as one rask row the owner
adds; "Ofc move storybook there aswell from this repo". Svelte Bits: its licence was read in full the same day, and to
the owner's question "Do we need svelte bits then? What does it give us?" the answer given was no. Landed here in
`8f7ab41`.

## Context

The workbench's copy of the design system (`@workbench/ui`) and rask's (`@rask/ui`) had diverged, and rask's console was
to take the workbench's look and theme picker. Svelte Bits is MIT with the Commons Clause, which forbids redistributing
its components "whether alone, in a bundle, or as a ported version". Baby UI is Apache-2.0.

## Decision

- Tokens, themes, components, the design document and the one Storybook live in `AI-Riksarkivet/ui`, package
  `@riksarkivet/ui`, which names neither rask nor the workbench.
- A component is changed there, never in an app.
- Nothing from Svelte Bits enters the UI repository. Baby UI components may be adapted, with their notice.

## Consequences

- `@workbench/ui` is frozen for the old apps and deleted with the last of them; it gets no change.
- Styling defects in shared components are fixed in the UI repository, then reach the app by a new pin (ADR 0016).
- The UI repository has no licence of its own yet (read from the GitHub API on 2026-11-20).
