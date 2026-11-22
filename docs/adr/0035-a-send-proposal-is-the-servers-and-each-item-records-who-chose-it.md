# 0035. A send's proposal is the server's, and each item records who chose it (2026-11-22)

**Status:** accepted. Refines 0027 (sampling orders a queue and selects items by a project's strategy). Source: the
owner, 2026-11-22, on #26 (WB-114): "least sure at send" comes from a score column only, with no model call at send.
The rest is the agent's that built it; the owner has not ruled on it.

## Context

0027 has the workbench propose which items of a dataset to send to a project, and show why an item was chosen so a
person can tell a sampled item from one they picked. A sender states which items to send; a sender able to state why
each was chosen could mark every item as sampling's choice, and the record would mean nothing.

## Decision

- **A proposal is asked for the candidates a person has in hand**, at most 1000, and how many to choose. The annotator
  reads the dataset as that person and ranks the candidates by the project's strategy: the highest doubt from the
  dataset's score column, and by k-center greedy over the embeddings, each pick the candidate farthest from the
  project's labelled items and from what is already chosen. `mix` takes from the two in turn. With nothing labelled
  yet, the first pick is the candidate farthest from the candidates' own mean.
- **No model is asked at send.** A project that ranks by doubt, sending from a dataset with no score column by its
  model, is told that least sure orders those items once they are in the project, and they are proposed by diversity.
- **The annotator keeps the proposal** for a day, under an id, for the person it was made for. A send may name it. The
  send then stamps each item `chosen`: by sampling, with the reason, the number behind it and the model that number
  comes from, for an item the proposal chose; by the person for every other. A send that names no proposal stamps
  every item as the person's. A send naming a proposal that is not the sender's for that project is refused whole.
- **A person can always override**: untick a proposed item, send the ones sampling did not choose, or ask for no
  proposal at all.

## Consequences

- `chosen` on an item is a fact the server wrote from its own record. An item sent before sends recorded it carries
  none, and is shown with no reason.
- A proposal's ranking costs one read of the candidates' score and embedding and of up to 500 labelled items'
  embeddings, in the sender's request.
- A proposal is a day's record: it is not a queue, and it reserves nothing. Two people proposing from the same
  candidates may be offered the same items.
