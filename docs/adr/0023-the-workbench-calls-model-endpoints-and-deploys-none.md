# 0023. The workbench calls model endpoints and deploys no model (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, asked where models run for assist and
semantic search: "endpoint ofc. we only connect".

## Context

Assist answers from a built-in stand-in; no real model is wired. An earlier note in rask (ADR 0137 there) said models
are served by Ray Serve from the owner's Ray cluster.

## Decision

The workbench is given the address of a model endpoint and calls it. It builds, deploys and scales no model server.

## Consequences

- A deployment without an endpoint has no assist and no semantic search, and says so.
- The endpoint's contract (request, response, authentication, timeouts) is the workbench's to state and to test against
  a stand-in.
