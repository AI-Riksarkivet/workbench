# 0011. Flows is parked (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18, through the question tool, "Park it for now": no screen, no
deployment, out of the plan until the labeling loop works on real data with a real model; nothing deleted.

## Context

Flows (model pipelines over datasets) has a service that keeps its runs in Postgres (ADR 0009), no chart workload, no
route on the Gateway and no screen in the app. Its two editors live in the old `frontend/studio` and
`frontend/explorer`'s `/workflow`. On 2026-11-14 the owner had decided to merge them into one flows area in the app
(issue #9, "merge flows and the explorer's graph flow into one").

## Decision

- Flows gets no screen and no deployment for now.
- Its code is not deleted.

## Consequences

- The merge of the two flow editors waits, and so does every row that needs flows: batch auto-label, the canvas's
  batch jobs, and #7's clause that a person with the run role starts a flow.
- Deleting `frontend/studio` and `frontend/explorer` (and with them the frozen `@workbench/ui`) conflicts with "nothing
  deleted" until the owner says otherwise.
