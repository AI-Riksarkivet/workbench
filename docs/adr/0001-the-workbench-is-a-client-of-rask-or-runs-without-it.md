# 0001. The workbench is a client of rask through its published contract, or runs without rask on any Lance source (2026-11-06, 2026-11-16)

**Status:** accepted. Source: the owner, 2026-11-06, "One workbench repo" (issue #1, which records the rulings on how
it reaches the lakehouse); the owner, 2026-11-16, "as a client for rask, or without rask with lance-namespace or a
Lance table directly", and through the question tool the same day, decision 4 on issue #9: read-only from any source,
sources declared in git, the rask-only behaviour behind one switch.

## Context

The workbench came out of rask's history at `64ef118a9` on 2026-11-14. Inside rask its parts imported rask's libraries,
used rask's sign-in and authorization, and read the lakehouse's store directly. A client that imports the lakehouse's
code breaks whenever the lakehouse changes, and one that reads its store bypasses its governance.

## Decision

- The workbench reaches a rask lakehouse only through what rask publishes: data through stock `lance_namespace` and
  pylance on the credentials the catalog vends, every other call through the catalog's and lineage's doors, and the
  events as rask documents them. Its copy of that contract is `docs/rask/`, pinned to a rask commit.
- It imports no rask code. What it once imported is its own copy, under its own names, changed freely.
- It reads the sources a deployment declares in git, each a name and a kind (`rask`, `namespace`, `tables`). rask is
  one optional source, at most one per deployment.
- Everything rask-only (the person's lakehouse token, the `x-lance-originator` and `x-rask-*` headers, the
  warehouse-first id rule, publish into rask) applies only to the `rask` source.

## Consequences

- A deployment with no lakehouse explores, searches and labels; publishing into rask is the only thing it cannot do
  (ADR 0004).
- Every other source is read-only and read with no token, confined to the names and locations it declares.
- Whenever rask removes or changes a door or a rule, `docs/rask/` is refreshed and the workbench moves with it in the
  same change.
