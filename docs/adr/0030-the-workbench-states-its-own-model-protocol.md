# 0030. The workbench states its own model protocol (2026-11-20)

**Status:** accepted; its adapter part (the second and third points of the decision, and the named first backend) is
replaced by ADR 0032. The protocol and the rule that the workbench states it stand. Source: the owner, 2026-11-20, in the grilling session, asked whether to speak X-AnyLabeling's
server protocol or define the workbench's own with an adapter per backend: "Our own protocol".

## Context

The workbench calls model endpoints and deploys none (ADR 0023). The owner named X-AnyLabeling as the reference for a
model backend, to sit behind Ray Serve later. Its server (X-AnyLabeling-Server, AGPL-3.0, read 2026-11-20) speaks
`GET /v1/models`, `POST /v1/predict {model, image, params}` answering `{shapes, description, replace}`, and video
sessions with a progress stream. It takes one image per call, keeps video sessions in the process, answers errors with
HTTP 200 and authenticates with one shared token.

## Decision

- The workbench defines the protocol it calls a model endpoint by: listing models and what each can do, asking for
  proposals on an item (with prompts where a model takes them), scores, the model version, errors by HTTP status,
  and an identity per caller.
- A backend is reached through an adapter that speaks that protocol to the workbench and the backend's own protocol to
  the backend. An adapter for X-AnyLabeling's server is one such; a Ray Serve deployment can speak the protocol
  directly.
- No code of X-AnyLabeling or its server is copied (GPL-3.0 and AGPL-3.0; the workbench is Apache-2.0, ADR 0024).

## Consequences

- The protocol is a contract the workbench publishes and tests against a stand-in, as it does the lakehouse's doors.
- An adapter is a separate small service or a module that imports nothing from the backend it fronts.
- Each model's weights carry their own licence, checked per model before real use.
