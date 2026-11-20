# 0027. Sampling orders a queue and selects items, by a project's strategy (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, on "active labeling and sampling": "Yes
please" to ordering the queue and choosing which items enter a project, with the strategy set per project and always
overridable; knowing when to stop is left for later.

## Context

Labelling everything is rarely worth it. A system can choose what is labelled next (the order of a queue), which items
are worth sending to a project at all, and when more labelling stops improving a model. The third needs a trained model
and an evaluation set.

## Decision

- A project sets a sampling strategy: the items a model is least sure about, the items least like those already
  labelled, or a mix.
- The strategy orders the project's queue, and proposes which items of a dataset to send.
- A person can always override it: send any item, label in any order.
- When to stop is not decided by the workbench for now.

## Consequences

- Uncertainty needs a score per item from a model endpoint; diversity needs embeddings. A project without either has no
  sampling and says so.
- Why an item was chosen is shown, so a person can tell a sampled item from one they picked.
