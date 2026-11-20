# 0021. The old frontends are deleted while flows is parked (2026-11-20)

**Status:** accepted; amends ADR 0011's "its code is not deleted" for the frontends only. Source: the owner, 2026-11-20,
in the grilling session, asked whether the old explorer's workflow editor, the old studio app and the frozen
design-system copy may be deleted with the flows service kept: "Please delete and remove."

## Context

Flows is parked (ADR 0011). Its two editors live in `frontend/studio` and `frontend/explorer`'s `/workflow`, neither
deployed. With `frontend/annotator` they are the only users of `frontend/packages/ui`, a frozen copy of the design
system the app no longer uses (ADR 0016). Every frontend gate still checks all of it.

## Decision

- `frontend/studio`, `frontend/explorer` and `frontend/packages/ui` are deleted, with what only they use.
- The flows service, its tests and its store stay.
- `frontend/annotator` is deleted when the app has fully live updates, as already planned.

## Consequences

- When flows resumes, its screen is built inside the one app, from git history where that helps.
- The frontend workspace holds the app and what it uses, and nothing else.
