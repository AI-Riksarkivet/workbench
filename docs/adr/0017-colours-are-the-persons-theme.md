# 0017. Colours are the person's theme (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18: the look follows a reference design system's structure (sharp
corners, one font weight, tight display type, uppercase labels, surfaces instead of borders, one layered shadow), "NOT
its colours: colours are a theme the user chooses themselves"; and through the question tool, "Document and evolve" for
the workbench's own look. Landed in `2a83eb1`.

## Context

After seeing the project screens, the owner found the app "very boring and shadcn like".

## Decision

- No brand palette is hard-coded: every colour is a token a theme sets.
- Five themes ship, each light and dark (Ink, Fjord, Paper, Moss, Contrast), and a person may pick an accent of their
  own, which is corrected until it reads at WCAG AA on every surface it is drawn on.
- The choice is kept in the person's browser and applied before the first paint, under the strict CSP.

## Consequences

- Every canvas that colours with a token redraws when the theme, mode or accent changes.
- The theme CSS needs relative colour syntax: Chromium 119, Firefox 128, Safari 16.4 at least; only Chromium is
  exercised.
