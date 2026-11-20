# 0013. Publish has three targets: a rask table, a Lance table on S3, export files (2026-11-18)

**Status:** accepted. Source: the owner, 2026-11-18, through the question tool, answering what happens after publish
in free text: "Export or a table in rask or s3". Read as: all three are wanted targets. Their order is not recorded.

## Context

Publish writes a frozen project's accepted work once, as a table in the lakehouse (`<warehouse>$published`), as the
workbench's own client. It has never completed against a real rask, and no other target exists.

## Decision

- A project's finished labels can go to a table in a rask lakehouse, to a Lance table on S3 outside rask, or out as
  export files.

## Consequences

- Which export formats, how an S3 target is declared, and the order the three are built in are open.
- A target outside rask names no lakehouse lineage; what it records instead is open.
