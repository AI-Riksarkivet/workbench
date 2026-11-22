# The workbench's model protocol, version 1

The workbench deploys no model (ADR 0023). A deployment gives it the address of a **model endpoint**, and the workbench
calls that endpoint by the protocol on this page (ADR 0030). A model backend is a Ray Serve deployment that speaks it
itself, with no adapter between them (ADR 0032; § A Ray Serve deployment).

Three things state the protocol, and a test holds them together:

- this page, for a person;
- `docs/model-protocol-openapi.json`, for a machine (`make model-protocol` writes it);
- `annotator.model_endpoint.protocol`, the models the workbench validates every ask and every answer with.

The stand-in endpoint (`annotator.model_endpoint.stand_in`) speaks it and holds no model. The suite, the local stack and
the development cluster run it. The suite also asks a Ray Serve deployment that speaks it (`tests/ray_model.py`).

## Who calls

Every call carries `Authorization: Bearer <token>`: the caller's own token. On a cluster it is the calling pod's
ServiceAccount token, projected by the kubelet for the endpoint's audience (the chart's `models.endpoint.audience`,
`workbench-models` by default), valid for an hour and read from its file on every call. An endpoint verifies it against
the cluster issuer's keys, and its subject against the annotator's ServiceAccount, and so knows which workload calls
(§ A Ray Serve deployment says how). There is no shared key.

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
      "returns": ["bbox"],
      "assesses": []
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
| `returns` | The shape types its proposals carry: `bbox`, `polygon`, `polyline`, `keypoint`, `mask`, `tag`, `text`, `segment`. Empty: it proposes nothing. |
| `assesses` | The shape types of the labels it judges (§ `POST /v1/assessments`). Empty: it judges nothing. |

A model lists at least one of `returns` and `assesses`.

The shape types are the workbench's own vocabulary. A deployment maps its model's names onto them.

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

## `POST /v1/assessments`

What one model disagrees with among the labels people made on one item: a judge (ADR 0026).

```json
{
  "model": "judge",
  "item": { "id": "demo~corpus/doc-1/0", "content": { "kind": "image", "mime": "image/png", "data": "<base64>" } },
  "labels": [
    { "id": "8c1f", "shape_type": "bbox", "x": 10, "y": 20, "width": 30, "height": 40, "polygon": [], "label": "stamp", "text": "" }
  ],
  "classes": ["seal", "stamp"],
  "requested_by": "dex~08a8684b"
}
```

- **`labels`** is every label of the item, each under the workbench's own id for it, in a proposal's geometry and
  words. All of them are sent, so a judge can also say one is missing. Empty: the person labelled nothing.
- `item`, `classes` and `requested_by` are an ask for proposals' own.

The answer:

```json
{
  "model": "judge",
  "version": "2026.11-a",
  "disagreements": [
    { "label_id": "8c1f", "reason": "the region holds a wax seal", "suggested_label": "seal", "score": 0.8 }
  ]
}
```

- **`label_id`** names a label of the ask; absent, the disagreement is about the item as a whole (a label that is
  missing). An answer naming a label the ask did not carry is outside the protocol.
- **`reason` is required**: what the judge disagrees with, as a reviewer reads it (at most 500 characters). A flag
  with no reason cannot be weighed.
- `suggested_label` is the class the judge would have given, or empty. `score`, from 0 to 1, is how sure it is.
- No disagreements is an answer: the labels look right. A judge that could not answer says so by its status.

A model that lists no `assesses` refuses the ask with 422, as one that lists no `returns` refuses an ask for
proposals.

## Errors

An error is an HTTP status with an RFC 9457 problem body (`application/problem+json`), never a 200 that says it failed.
`GET /v1/models` answers 401, 403, 429, 503 or another 5xx; `POST /v1/proposals` and `POST /v1/assessments` any status below. The OpenAPI
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

## What the workbench does with an assessment

- A project names its judge (a setting); a project that names none has none. The judge is asked about an item in
  review, once per submission, in the request of the person who submitted it or of someone who may review it: the
  item's picture is read as that person, and the annotator holds no credential that reads it for nobody.
- The verdict is kept beside the item with the model and version that gave it and the revision of the work it read. It
  orders and marks the review list (flagged first) and changes no item's state: a reviewer accepts or sends back every
  item (ADR 0026).
- A reviewer's decision on a flagged item is kept with the verdict, with whether they agreed with the flag: what they
  said, else what the decision says (accepting the work as it is disagrees; fixing it or sending it back agrees).
- A judge that fails, is busy or does not answer is the error the asking person reads, and the item stays unjudged;
  the review list reads as it does with no judge.

## Bounded in this version

- Assist sends a picture: a unit's frame, or a whole document's thumbnail. The protocol carries text; the workbench
  does not yet ask about a text item, a recording or a video.
- One endpoint per deployment.
- A judge is shown a picture, as assist is, and reads an item's labels and not the links between them.
- The stand-in accepts any bearer token and refuses a call with none. Verifying whom a token names is a real
  endpoint's.

## A Ray Serve deployment

A model backend is a Ray Serve deployment that answers the two doors under its route prefix (ADR 0032). With
`route_prefix="/models"` behind Serve's HTTP proxy, `models.endpoint.url` is `http://<rayservice>-serve-svc:8000/models`,
and the workbench asks `/models/v1/models` and `/models/v1/proposals`.

### Who calls: the deployment checks it

**Serve verifies no token.** Its proxy hands the `Authorization` header to the replica untouched, so a deployment
verifies every call itself, and refuses with a problem body:

- **401** unless the bearer token is signed by a key the cluster's issuer publishes (RS256), names that issuer
  (`iss`), the audience the chart's `models.endpoint.audience` names (`aud`, `workbench-models` by default), and
  carries `exp`, `iat` and `sub`, judged with a small clock skew (30 s in the suite). A token missing any of these
  claims is refused, not taken as never expiring.
- **403** when the token passes and its `sub` is not the annotator's ServiceAccount,
  `system:serviceaccount:<release namespace>:<release>-annotator` (`system:serviceaccount:workbench:workbench-annotator`
  on the development cluster). The audience alone says nothing about who calls: any pod may project a ServiceAccount
  token for any audience.
- **The key set:** read from the issuer's OIDC discovery (`<issuer>/.well-known/openid-configuration`, its
  `jwks_uri`; on Kubernetes the API server's `/openid/v1/jwks`, which the deployment's ServiceAccount must be allowed
  to read: the `system:service-account-issuer-discovery` ClusterRole), and read again when a token names a key the
  set held does not have, at most once in a short while (a second in the suite), since a cluster rotates its signing
  key.

### What the suite asks of one

`tests/ray_model.py` is such a deployment with fixed answers, run on a Ray the annotator's endpoint tests start
(`services/annotator/tests/test_assist_asks_a_model_endpoint.py`). Through the annotator's own doors, each of these is
a test that fails if it stops being true:

- assist gets the deployment's proposal, naming its model and version, from the largest picture the protocol carries
  (16 MiB, 22 MB as base64, through Serve's proxy): the deployment answers the picture's SHA-256 as it decoded it, and
  the test compares it;
- a replica that holds an ask past the workbench's deadline is the person's timeout; one that answers 503 with
  `Retry-After` is busy, with the same `Retry-After`; one that answers 500 is a failure in its own words;
- the annotator's token is taken, and so is one its issuer's new key signed after a rotation; a token that is expired,
  has no expiry, names another audience or another issuer, or whose claims were changed is refused, and the person
  reads that the endpoint refused the workbench's identity (401); another workload's token is refused 403, read the
  same way.

Not asked of it: an assessment (`tests/ray_model.py` serves no judge), the listing through the annotator, an unknown model, an ask outside the protocol, the answer cap, and
a redirect. The stand-in's tests pin the annotator's side of each, which does not depend on what serves the protocol.

### Measured once, not pinned by a test

On this host (aarch64, Ray 2.60.0, Python 3.13), 2026-11-20:

- **Cost:** the `ray` package is 212 MB installed, its environment with its dependencies about 390 MB; a local Ray
  starts in about 2.6 s and Serve with an app in about 2.0 s more (about 6 s inside the suite, with Ray's workers
  starting), its driver about 260 MB of memory; a shutdown takes about 4 s. `ray[serve]` is a dev dependency, in no
  image.
- **Architectures:** wheels exist for linux aarch64 and x86-64; the suite runs it on aarch64 here and in the Dagger
  lane, and on x86-64 in CI.
- **Serve's own backpressure:** a deployment holding `max_ongoing_requests=1` and `max_queued_requests=1`, asked four
  times at once, answered two and dropped two with 503 "Request dropped due to backpressure", plain text, with no
  `Retry-After`. The annotator reads any 503 as busy, so the person reads that the endpoint is busy for now. Not
  pinned: which asks a queue drops depends on when each arrives. `max_queued_requests` cannot be 0.
- **Started under `uv run`**, Ray starts each worker through `uv run` too, on an uploaded copy of the working directory,
  and none started; `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` makes the workers use the driver's interpreter. The suite sets it
  in the root `conftest.py`.

Not shown yet: a deployment on the development cluster, which has the KubeRay definitions and runs no Ray.

## The stand-in's models

| Model | Behaviour |
| --- | --- |
| `stand-in-detector` | A box where the person drew one (else a fixed box), labelled with the task's first class, score 0.7. |
| `stand-in-segmenter` | A polygon around the clicks or the box; each click raises the score. |
| `stand-in-slow` | Never finishes its answer (a byte now and then): the person reads the timeout. |
| `stand-in-failing` | Answers 500: the person reads the error. |
| `stand-in-tagger` | Takes no prompt: one tag about the whole item, labelled with the task's first class, score 0.6. |
| `stand-in-judge` | Proposes nothing. Disagrees with every label of the task's last class (it would say the first, score 0.8), and with an item nobody labelled (score 0.5). |

`stand-in-slow` and `stand-in-failing` assess as they propose: never, and with a 500.

`python -m annotator.model_endpoint.stand_in` serves it (`MODEL_STAND_IN_HOST`, `MODEL_STAND_IN_PORT`; 127.0.0.1:8120).
`make local` runs it on port 8706, and `make deploy-dev` installs it from `deploy/fixtures/model-stand-in`.
