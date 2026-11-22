# 0034. A sampling number is read or asked in a person's request, and kept beside the task (2026-11-22)

**Status:** accepted. Refines 0027 (sampling orders a queue and selects items by a project's strategy). Source: the
owner, 2026-11-22, on #26 (WB-114): least-sure ordering, least-similar ordering and the send proposal ship as one row,
and where a score comes from is the design's to choose "for what is scalable, fault tolerant and cloud native". The
choices below are the agent's that built it; the owner has not ruled on them one by one.

## Context

0027 orders a project's queue by how unsure a model is of each item, by how unlike the labelled items each one is, or
by both. The first needs a score per item and the second an embedding per item. The annotator reads a dataset as the
person asking and holds no credential that reads one for nobody (0033), so no background job of the workbench can
score or measure a project's items. A model call per item made at the moment a list is read does not scale past a few
dozen items, and loses its work when a pod restarts.

## Decision

- **The score that scales is a column in the dataset.** A pipeline on the owner's compute writes a model's confidence
  per row, and the dataset's descriptor names the column and the model (`sampling.scores`). The pipeline retries, the
  result is durable in the table, and the workbench only reads it. A project that samples by that model reads its
  items' scores from the column and asks no model for them.
- **Items the column does not score are topped up in the Label tab**, a bounded number per visit (50, four asks in
  flight), each ask in the visiting person's request through the model protocol's `POST /v1/proposals`. Then it stops,
  says how many remain and offers to score more. A model that fails stops the round and is said; the queue keeps
  working in whatever order it has. An item a round could not score (it is no picture, or the model refused it) is
  passed over by the next round, so a round always reaches items not tried yet.
- **Doubt is 1 less the model's confidence**: the column's number, or the highest score among the proposals the item's
  classes take. A model that proposed nothing the item takes is recorded as such, and such items come after the scored
  ones.
- **Diversity is measured, not asked.** An item's novelty is the cosine distance from its embedding to the nearest of
  the project's labelled items of the same dataset (the latest 500 submitted), in the vectors the dataset declares:
  `sampling.embeddings`, else its default search's first vector binding. Declaring embeddings for sampling adds no
  search mode, so a dataset needs no query encoder to be ranked. A dataset that declares neither says diversity is
  unavailable for its items; one with no labelled item yet says there is nothing to measure against.
- **Both numbers are rows of their own beside the task** (`samplings`, `novelties`), written by doors of their own that
  never touch a task, its claim or its draft, as a judge's verdicts are (0033). A score records the model that gave it,
  and one by another model than the project names now reads as no score. A novelty records the vector column and the
  model its dataset says made the vectors, and how many labelled items it was measured against; it is replaced each
  time the item is measured again, which a person asks for.
- **The strategy is read when a list is ordered**, never captured on an item: `off`, `least_sure`, `least_alike` or
  `mix`, and the model, change until a publish starts. `mix` interleaves the two orders over the items a filter
  matches, doubt first, and each listed item says which of the two placed it. A listing says when the strategy orders
  none of its items and what is missing: no model named, no endpoint to ask, nothing measured.
- **Scoring and measuring take the annotating relation**: someone who may only look at a project spends no model and no
  read on it. Whoever may view an item reads its numbers, since they say nothing of anyone's labels.

## Consequences

- No credential reads a dataset for nobody, and every score and every measure names the person whose request made it.
- A project whose dataset carries a score column is ordered on the first visit with one read and no model call. One
  without is ordered 50 items a visit, which is slow for a large project: the answer is the column, not a larger
  top-up.
- A novelty is as old as its last measure. Labelling more items does not move the order until someone measures again.
- A comparison across two models' vectors cannot arise: items are compared only within one dataset's one declared
  column. The model is recorded so a reader can tell which vectors a number came from, and a dataset re-embedded by
  another model is measured again by asking.
- Not built: scores for text, audio or video items from the endpoint (a sampling model is shown a picture, as assist
  and a judge are; a score column covers any item); deciding when to stop labelling (0027).
