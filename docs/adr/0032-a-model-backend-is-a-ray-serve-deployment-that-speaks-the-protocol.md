# 0032. A model backend is a Ray Serve deployment that speaks the protocol (2026-11-20)

**Status:** accepted; replaces the adapter part of ADR 0030. Source: the owner, 2026-11-20, after the protocol shipped
(WB-112), asked which backend the first model slice should prove: "We use Ray serve models", and of an adapter in
front of another labeling tool's server: "I don't wanna use" it; of what the next row should test: "Why not just test
with Ray serve directly for testing?"

## Context

ADR 0030 says a backend is reached through an adapter per backend, names an adapter for another labeling tool's server
as the first, and says a Ray Serve deployment can speak the protocol directly. WB-113 was written to build that first
adapter and put "one real model" behind assist. The protocol and a stand-in that speaks it are in place
(`docs/model-protocol.md`, `annotator.model_endpoint.stand_in`).

## Decision

- The workbench's model backends are Ray Serve deployments. A deployment speaks the workbench's model protocol itself;
  no adapter service stands between them.
- No adapter for another labeling tool's server is built, and none of its code or protocol is taken as a reference.
- The model server the workbench once built and deployed itself (`runners/assist`, removed by WB-112) is not revived.
- What the next model row proves is the Ray Serve path, not a model's quality: a deployment that answers the protocol,
  whose answers may be fixed. A real model replaces it by deploying, with no change in the workbench.

## Consequences

- ADR 0030 stands for the protocol itself and for the rule that the workbench states it. Its sentences about adapters
  and the named first backend no longer hold.
- The workbench still deploys no model (ADR 0023): a Ray Serve instance on a development cluster is a fixture beside
  the release, and in production it is the platform's or the compute repository's.
- Ray Serve verifies no caller's token by itself, so who checks the annotator's token on that side is part of what the
  row settles.
