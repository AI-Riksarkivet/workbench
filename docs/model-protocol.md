# The workbench's model protocol, version 1

The workbench deploys no model (ADR 0023). A deployment gives it the address of a **model endpoint**, and the workbench
calls that endpoint by the protocol on this page (ADR 0030). A backend that speaks another protocol is reached through
an adapter: a small service that speaks this protocol to the workbench and the backend's own to the backend.

Three things state the protocol, and a test holds them together:

- this page, for a person;
- `docs/model-protocol-openapi.json`, for a machine (`make model-protocol` writes it);
- `annotator.model_endpoint.protocol`, the models the workbench validates every ask and every answer with.

The stand-in endpoint (`annotator.model_endpoint.stand_in`) speaks it and holds no model. The suite, the local stack and
the development cluster run it.

## Who calls

Every call carries `Authorization: Bearer <token>`: the caller's own token. On a cluster it is the calling pod's
ServiceAccount token, projected by the kubelet for the endpoint's audience (the chart's `models.endpoint.audience`,
`workbench-models` by default), valid for an hour and read from its file on every call. An endpoint verifies it against
the cluster issuer's keys and so knows which workload calls. There is no shared key.

`requested_by` in an ask names the person the ask is made for, as the workbench names them. It is for the endpoint's
own record and is not a credential.

The workbench follows no redirect: the token is presented to the address the deployment named and to no other.

## `GET /v1/models`

The models the endpoint serves and what each can do.

```json
{
  "protocol": "1",
  "models": [
    {
      "name": "detector",
      "version": "2026.11-a",
      "title": "Seal detector",
      "reads": ["image"],
      "prompts": ["text", "box"],
      "returns": ["bbox"]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `name` | What an ask names the model by. |
| `version` | Which version answers. Every proposal records it, so two versions can be compared. |
| `title` | What a person is shown; empty shows the name. |
| `reads` | What the model reads of an item: `image`, `text`. |
| `prompts` | What a person may add: `text` (words), `box` (a box drawn on a picture), `points` (clicks). Empty: none. |
| `returns` | The shape types its proposals carry: `bbox`, `polygon`, `polyline`, `keypoint`, `mask`, `tag`, `text`, `segment`. |

The shape types are the workbench's own vocabulary. An adapter maps its backend's names onto them.

## `POST /v1/proposals`

One model's proposals on one item.

```json
{
  "model": "detector",
  "item": { "id": "demo~corpus/doc-1/0", "content": { "kind": "image", "mime": "image/png", "data": "<base64>" } },
  "prompts": { "text": "a seal", "box": { "x": 10, "y": 20, "width": 30, "height": 40 }, "points": [] },
  "classes": ["seal", "stamp"],
  "requested_by": "dex~08a8684b"
}
```

- **The item's content is carried in the ask.** A picture is its bytes in base64 with its media type (at most 16 MiB); a
  text is the text. The endpoint fetches nothing and needs no way back to the workbench, so it can run anywhere. `id` is
  the workbench's own name for the item, opaque to the endpoint.
- **`prompts`** carries only the kinds the model lists. `points` is every click of the session so far, in order
  (`positive: false` says "not this"): the ask is the whole session, and the endpoint keeps none.
- **`classes`** are the classes the item's task labels with. A model that can label in a given vocabulary uses these
  names; one that cannot ignores them.

The answer:

```json
{
  "model": "detector",
  "version": "2026.11-a",
  "proposals": [
    { "shape_type": "bbox", "x": 10, "y": 20, "width": 30, "height": 40, "polygon": [], "label": "seal", "text": "", "score": 0.83 }
  ]
}
```

- Coordinates are the item's own: a picture's pixels, a text's code points (`char_start`, `char_end`).
- **`score` is required**, from 0 to 1: how sure the model is. A proposal with no score cannot be ranked or held to a
  threshold.
- No proposals is an answer: the model found nothing. A model that could not answer says so by its status.

## Errors

An error is an HTTP status with an RFC 9457 problem body (`application/problem+json`), never a 200 that says it failed.
`GET /v1/models` answers 401, 403, 429, 503 or another 5xx; `POST /v1/proposals` any status below. The OpenAPI
document declares the same statuses on each door.

| Status | Meaning | What the workbench tells the person |
| --- | --- | --- |
| 401, 403 | No token, one the endpoint does not accept, or a caller that may not ask this model. | 502: the endpoint refused the workbench's own identity. |
| 404 | The endpoint serves no model of that name. | 404. |
| 400, 413, 422 | The ask is outside the protocol, too large, or carries a content or prompt kind the model does not read. | 422, with the endpoint's `detail`. |
| 429, 503 | Busy or unavailable for now; `Retry-After` says when to ask again. | 503, with the endpoint's `Retry-After`. |
| any other | The model failed. | 502, with the endpoint's `detail`. |

The workbench waits `models.endpoint.timeoutSeconds` (30 by default) for the whole answer to an ask for proposals,
and 5 seconds for the list of models; it reads at most 32 MiB of an answer. An endpoint that does not answer in time is a 504 to the person. An answer that does not fit this page (an
unknown field, a shape type outside the vocabulary, a score outside 0 to 1, another model's name) is a 502. None of
these is ever shown as "no proposals".

A deployment that names no endpoint has no assist: the list of models answers `available: false` with the reason, and
an ask is a 503.

## What the workbench does with an answer

- It refuses, and says, a proposal whose shape type none of the task's classes takes; it carries no label that is not
  one of the task's classes.
- A proposal is saved as a shape with status `prediction`, naming its model (`source: model:<name>`), the model's
  version and its score. A save of a model's shape lacking the version or the score is refused, and so is a proposal
  that names no model.
- A proposal is a person's to settle. An item holding one that nobody kept or removed cannot be submitted, and a
  reviewer cannot accept it (ADR 0019);
  keeping or removing all of an item's proposals is one action. A kept proposal still says which model made it. One a person rejected stays in the
  item's work as rejected and is not published.

## Bounded in this version

- Assist sends a picture: a unit's frame, or a whole document's thumbnail. The protocol carries text; the workbench
  does not yet ask about a text item, a recording or a video.
- One endpoint per deployment.
- The stand-in accepts any bearer token and refuses a call with none. Verifying whom a token names is a real
  endpoint's.

## The stand-in's models

| Model | Behaviour |
| --- | --- |
| `stand-in-detector` | A box where the person drew one (else a fixed box), labelled with the task's first class, score 0.7. |
| `stand-in-segmenter` | A polygon around the clicks or the box; each click raises the score. |
| `stand-in-slow` | Never finishes its answer (a byte now and then): the person reads the timeout. |
| `stand-in-failing` | Answers 500: the person reads the error. |

`python -m annotator.model_endpoint.stand_in` serves it (`MODEL_STAND_IN_HOST`, `MODEL_STAND_IN_PORT`; 127.0.0.1:8120).
`make local` runs it on port 8706, and `make deploy-dev` installs it from `deploy/fixtures/model-stand-in`.
