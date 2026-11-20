# 0004. Work in progress lives in the annotator's Postgres until publish (2026-11-17)

**Status:** accepted. Source: the owner, 2026-11-17, through the question tool, "Postgres until publish", after two
written analyses of where working labels live. It followed the owner's 2026-11-16 decision that the annotator moves off
Dapr with its store chosen by best practice (ADR 0009). Landed in `bcbec0e`.

## Context

Until then each project kept its work twice: the task's draft in the annotator's Postgres, and a per-project Lance
working table in the lakehouse (`acme$workbench_annotations`) that the canvas wrote. The reasons given in the analysis
the owner decided on: everything but the canvas already read the Postgres draft; the two copies drifted; the Lance
check was project-wide; consensus replicas mixed in one table; and the working table was the only reason labeling needed
a lakehouse.

## Decision

- The labels people make live only in the annotator's Postgres, in each task's draft, until the project is published.
- The lakehouse is written once, at publish, through the catalog's doors, as the workbench's own client.
- The per-project Lance working table and its code are deleted.

## Consequences

- Labeling needs no lakehouse; a deployment with none says plainly at publish that there is nowhere to publish to.
- The workbench holds no write grant in a lakehouse while people label (the dev grant was revoked on 2026-11-17).
- The annotator's Postgres holds the only copy of unpublished work, so its durability is the workbench's concern
  (backup, instances, schema changes).
