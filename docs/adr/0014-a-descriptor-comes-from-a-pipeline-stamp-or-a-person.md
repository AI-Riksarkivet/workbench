# 0014. A table's descriptor comes from a pipeline stamp or from a person in the app (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18, through the question tool, "Both", after first answering the
question with a question ("A person or the pipeline?"). Not built yet.

## Context

Only a dataset with a descriptor can be sent to a project, and the descriptor comes only from a `lance_media.descriptor`
stamp. Only the demo seed writes one, so no real lakehouse table can be labeled.

## Decision

- A stamp written by the pipeline that made the table is used when present.
- Otherwise a person picks the columns in the app (the row identity and the media, as a set-up picks the atlas's), and
  the workbench keeps that set-up.

## Consequences

- Where the workbench keeps a person's descriptor, and who may make one, is decided in the row that builds it.
- Nothing is written to the source table.
