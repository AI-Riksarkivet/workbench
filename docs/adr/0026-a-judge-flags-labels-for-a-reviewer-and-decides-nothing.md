# 0026. A judge flags labels for a reviewer and decides nothing (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session: the workbench does "ai labeling, ai as
judge"; of three meanings offered, "Yes looks correct" to: a model reviews people's labels and flags the ones it
disagrees with, first; a model accepting labels above a threshold in a reviewer's place, later, as a per-project
setting.

## Context

"AI as judge" can mean a model that flags people's labels for a reviewer, a model that accepts labels in the reviewer's
place, or a model that scores other models' proposals. Neither reference the owner named (X-AnyLabeling,
ActiveLabelingSystem) has a judge; ActiveLabelingSystem accepts labels above a confidence threshold and sends a random
share to a person.

## Decision

- A judge is a model that assesses the labels on a submitted item and flags the ones it disagrees with. It orders and
  marks the review queue. A reviewer still accepts or sends back every item.
- Accepting labels without a reviewer, above a threshold, is a later per-project setting, not the default, and is
  decided separately.

## Consequences

- With a judge on, every accepted label is still a person's decision.
- A flag must say what the judge disagreed with and which model version judged, so a reviewer can weigh it.
