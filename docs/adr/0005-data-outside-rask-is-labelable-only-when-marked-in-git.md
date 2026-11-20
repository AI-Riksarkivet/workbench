# 0005. Data outside rask is labelable only when its source is marked annotatable in git (2026-11-17)

**Status:** accepted. Source: the owner, 2026-11-17, through the question tool, "Only if marked". It superseded the
same day's "Any source can be annotated", which had lifted the 2026-11-16 rule that only rask data is annotated.

## Context

Once the workbench read any Lance source (ADR 0001), the question was whether people may label data that is not stored
in rask. The cluster needed data to label: no rask table carried a workbench descriptor.

## Decision

- A lakehouse's datasets are always annotatable.
- Any other source's datasets are annotatable only when git marks the source `annotatable: true`.
- One function decides it, and every place that allows or refuses labeling reads it.

## Consequences

- Each item records its table and version at send, for every source, and every published row names them.
- A publish names only the lakehouse's tables as lineage inputs.
- The dev cluster's demo source is marked annotatable; it is read-only like every source but a lakehouse.
