# 0019. An item with an unconfirmed proposal cannot be submitted (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, asked whether an item whose only label is
a model's unconfirmed proposal may be submitted and published: "What best practices i guess", taking the
recommendation.

## Context

A model's answer is kept on an item as a proposal: a shape whose status is `prediction`. A person keeps it (its status
becomes accepted, with that person recorded) or removes it. At `202d2e3` submit judges only each shape's class against
the ontology, so an item whose only label is a proposal can be submitted, accepted and published as a label.

## Decision

An item cannot be submitted for review while it holds a proposal nobody kept or removed. The refusal names how many
proposals are left.

## Consequences

- A published label is always a person's judgement, or a model's that a person kept.
- The Label grid and the canvas must make keeping or removing every proposal of an item one action.
- A project that wants model output published unreviewed has no way to do it here; that is a pipeline's job, writing
  through the lakehouse's own doors.
