# 0009. The workbench runs no Dapr (2026-11-16)

**Status:** accepted. Source: the owner, 2026-11-16, through the question tool (issue #9): "The annotator moves off
Dapr. Projects and tasks are kept by the annotator itself, with no sidecar", with the store chosen by best practice. The
annotator's move landed in `afc9028`; flows moved its runs into its own Postgres in `dbe9f8a` (2026-11-17), a choice made
under that ruling (who chose it beyond the commit is not recorded).

## Context

The annotator kept its projects, tasks and roles in Dapr actors, and its chart refused to render without Dapr. The dev
cluster runs no Dapr: rask removed it (rask's ADR 0207).

## Decision

- No workbench service runs a Dapr sidecar, imports Dapr or needs a Dapr object.
- The annotator keeps projects, tasks, claims, drafts and history in its own PostgreSQL (psycopg 3, plain SQL).
- Flows keeps each run and each step's outcome in its own PostgreSQL, driven under a lease by any replica.
- Durable work is a lease on a Postgres row, renewed by its driver and taken over by any replica's re-drive.

## Consequences

- No queue, actor or workflow engine sits beside the services; each service's state is one database of its own.
- Prose that still describes a sidecar, actors or reminders is wrong and is removed when found.
