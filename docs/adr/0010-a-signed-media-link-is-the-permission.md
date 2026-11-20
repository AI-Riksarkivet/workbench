# 0010. A signed media link is the permission (2026-11-14)

**Status:** accepted. Source: the owner, 2026-11-14, through the question tool (issue #9), in three steps the same day:
first both a presigned and a proxy mode per deployment; then, after a design review, "Proxy by default, with an
immutable cache", with a presigned link only for External blobs and no chart mode; then "a signed media link is the
permission".

## Context

`<img>` and `<video>` send no token, and the app's token lives in the browser's memory. Lance stores small blobs inline
or packed in a shared file, as byte ranges, which no store-presigned link can address.

## Decision

- `POST /api/signed-media` checks the person's own read of that table at that version, then signs a link for that one
  cell, valid for 5 minutes, with a key from a mounted file.
- `GET` on the link reads that one cell as the viewer's own identity, not the person's: the link is the permission.
- Only an External blob, which carries its own URI, may be answered with a store-presigned link.

## Consequences

- It works with any number of viewer replicas, with no shared token store and no session affinity.
- A stolen link shows one cell until it expires, as a presigned link would.
- The viewer's identity holds a read grant in rask on the datasets it serves, made at rask's access doors from the
  workbench's own values.
