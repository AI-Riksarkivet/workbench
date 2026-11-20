# 0028. The learning loop trains outside the workbench (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, on "reinforcement learning": "Yes sound
right" to a model that improves from people's corrections through periodic training; and "Correct" to the workbench
only publishing labels, with a pipeline on the owner's compute training from the published table.

## Context

"Reinforcement learning" was named as a purpose. Three readings were offered: a model improving from people's
corrections, a sampler that learns what to ask, and training a model with rewards from a judge. The workbench calls
model endpoints and deploys none (ADR 0023), and the lakehouse runs no training either: pipelines are the user's.

## Decision

- The first goal is a loop: people keep, change or remove proposals; the result is published; a pipeline outside the
  workbench trains on the published labels; the new model version answers at the model endpoint; its proposals come
  back.
- The workbench starts no training and holds no trainer.
- Every proposal records the model version that made it.
- A sampler that learns what to ask, and training with rewards from a judge, are directions, not planned work.

## Consequences

- The loop cannot close before publishing to the lakehouse works on the cluster.
- Comparing two model versions (how many proposals people kept, changed or removed) is the workbench's to show.
