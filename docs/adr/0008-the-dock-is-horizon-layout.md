# 0008. The dock is horizon-layout, in the app's own code (2026-11-14)

**Status:** accepted. Source: the owner, 2026-11-14, through the question tool (issue #9): the horizon-layout dock is
copied into the one app's `lib/dock` from rask's console, with no shared package; the owner, the same day, "Please
remove dockview". The library choice is rask's ADR 0200 (owner, 2026-11-11, "Please use horizon layout"). The move to
horizon-layout 2.0.2 without a patch landed in `2a83eb1` (2026-11-18); the owner's own words on 2.0.2 were relayed, not
recorded here.

## Context

The extracted explorer docked its panels on dockview 7, the last 7.x; dockview 8 moved keyboard navigation into a
separately licensed module.

## Decision

- The dock is horizon-layout, pinned exactly in `frontend/app/package.json` (2.0.2, unpatched), in the app's own code
  under `#lib/dock`: the layout value and its storage, autosave, saved views, the frame and its theme mapping.
- Dockview is gone, and no commit holds both docks.
- A dataset is one dock; every view of it is a panel.

## Consequences

- A panel is rebuilt when it is moved, maximized, popped out or its tab is switched, so a panel keeps no state that must
  survive that in its component.
- Layouts and saved views are kept per person in this browser.
