# 0025. Live updates cover a project's screens and the canvas; dataset pages wait (2026-11-20)

**Status:** accepted; sets the scope of ADR 0012. Source: the owner, 2026-11-20, in the grilling session: "we can do this
for now", after asking whether listening to the lakehouse's events later would couple the two too tightly.

## Context

ADR 0012 says other people's changes appear as they happen. Two things could be live: a project's screens and canvas,
where two people can act on the same item, and dataset pages, where a pipeline's new commit could appear by itself.
The second needs the workbench to take the lakehouse's commit events.

## Decision

- Live updates cover a project's screens and the labeling canvas.
- Dataset pages keep "refresh to see new data".

## Consequences

- Dataset pages can become live later without coupling the two products more tightly than they are: the lakehouse
  publishes its events under a stated contract for any client (`docs/rask/COMMIT-EVENTS.md`), and a client receives
  them on an identity of its own. The workbench would depend on that contract, as it depends on the lakehouse's doors
  today, and on nothing inside the lakehouse.
