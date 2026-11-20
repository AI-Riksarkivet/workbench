# 0031. A project may accept confident proposals, with a sample checked by a person (2026-11-20)

**Status:** accepted; amends ADR 0019 for projects that switch it on. Source: the owner, 2026-11-20, in the grilling
session, asked whether proposals above a confidence threshold may be accepted automatically with a random share still
sent to a person, as a per-project setting that is off by default: "Yes".

## Context

ADR 0019 says an item with an unconfirmed proposal cannot be submitted, so every published label is a person's
judgement or a model's that a person kept. ADR 0026 leaves accepting without a reviewer to a later per-project setting.
The owner's reference ActiveLabelingSystem accepts above a threshold and sends a share (1% there) to a person.

## Decision

- A project may switch on automatic acceptance: a proposal at or above the project's threshold is accepted without a
  person, and a random share of those, at the project's sample rate, still goes to a person to check.
- It is off by default. The threshold and the sample rate are the project's settings and are shown.
- A label accepted this way is marked as accepted by a model, with the model version and its score, wherever the label
  is shown and in what is published.

## Consequences

- In a project with it on, a published label can be one no person looked at; a consumer can tell which from the mark.
- What the checked sample finds (how often a person disagreed) is shown per model version, or the setting cannot be
  trusted.
