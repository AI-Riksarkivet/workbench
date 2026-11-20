# 0029. A first model slice comes before the loop on the lakehouse (2026-11-20)

**Status:** accepted; changes the order agreed earlier the same day. Source: the owner, 2026-11-20, in the grilling
session, asked whether the model work should move earlier now that it is a core purpose: "I think yes".

## Context

The order agreed earlier on 2026-11-20 put "a real model behind assist" last, after hardening.

## Decision

The order of work is:

1. Finish the screens (the walk's defects, the styling pass, live updates, deleting the old frontends).
2. Back up the annotator's database.
3. A first model slice: one real model endpoint behind assist, and queues ordered by the uncertainty its scores give.
4. The loop on the lakehouse: demo data loaded into the lakehouse, a descriptor a person sets, publishing on the
   cluster.
5. The judge, and the learning loop's side of the workbench (model versions, their comparison).
6. The other publish targets.
7. Hardening.
