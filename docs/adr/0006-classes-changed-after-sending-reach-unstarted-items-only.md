# 0006. Classes changed after sending reach only items nobody has started (2026-11-17)

**Status:** accepted. Source: the owner, 2026-11-17, through the question tool, "Update unstarted items". Landed in
`bcbec0e`.

## Context

A task keeps the ontology its project had when the item was sent. A cluster walk found that a project created with "Create
and send", which then had no classes, left its items with nothing to label with.

## Decision

- When a project's classes change, items nobody has labeled take the new classes; started or finished items keep the
  classes they were sent with.
- "Create and send" asks for classes before it sends.

## Consequences

- An item's labels are always judged against the classes it was labeled under.
- A publish stamps the project's current ontology (not recorded whether this should name each item's own; it is a
  question for publish's own row).
