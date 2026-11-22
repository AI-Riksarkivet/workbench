# 0033. A judge is asked in a person's request, once per submission (2026-11-22)

**Status:** accepted. Refines 0026 (a judge flags labels for a reviewer and decides nothing). Source: a technical choice
made while building #29 (WB-117), 2026-11-22, by the agent that built it; the owner has not ruled on it.

## Context

A judge reads an item's picture and the labels submitted on it. The annotator reads a dataset as the person asking
(`identity: person`, the default for a lakehouse source): it exchanges or passes on that person's token and holds no
credential that reads a dataset for nobody. A runner that judged submissions in the background would have no person to
read as. Giving the annotator's own identity a standing read on every dataset a project labels would be a credential
trusted by position, and on the development cluster it needs a grant rask's install does not yet take from a client
(#39).

## Decision

- The judge is asked through a door of its own, in the request of the person who submitted the item or of someone who
  may review it, and the picture is read as that person. The app asks when a submit lands in review, and the Review
  list asks for the listed items no judge has read yet, a few at a time.
- A submission is judged once. The verdict is a row of its own, keyed by the task and the instant it was submitted,
  recording the model, its version and the revision of the work it read. A resubmitted item has no verdict until it is
  judged again, and each submission's verdict is kept with what its reviewer decided.
- The judge's door reads the task and never writes it. The reviewer's decision is the one write that touches both: it
  moves the task and, in the same transaction, records on the verdict who decided, which decision, and on a flagged
  item whether they agreed with the flag.
- Whether the reviewer agreed is what they say, else what the decision says: accepting the work as it is disagrees,
  fixing it or sending it back agrees. Asking costs a reviewer no extra step.
- The protocol gains `POST /v1/assessments` and `assesses` on a model (`docs/model-protocol.md`), under version 1: no
  endpoint outside this repository speaks the protocol yet.

## Consequences

- No credential reads a dataset for nobody, and every ask of a judge names the person it was made for.
- An item submitted by a client that never asks is judged when a reviewer's list asks for it, so the top of a review
  list is judged before a reviewer works through it; items further down may be unjudged until their page is read.
- A judge costs one model call per submission, attributable to a person.
- Not built: judging text, audio or video items (a judge is shown a picture, as assist is); accepting without a
  reviewer above a threshold (0031, #30).
