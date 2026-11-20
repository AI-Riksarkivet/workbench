"""A stand-in model endpoint: it speaks the workbench's model protocol and holds no model.

For the suite, the local stack and a development cluster, where it is what assist is proven against (ADR 0023: the
protocol is tested against a stand-in). Its answers are computed from the ask alone, so the same ask always gets the
same answer. Each way an endpoint can behave is a model of its own, so a person can meet every one from the canvas:

- ``stand-in-detector``: a box where the person drew one (else a fixed box), labelled with the first class or the prompt.
- ``stand-in-segmenter``: a polygon around the clicks or the box.
- ``stand-in-slow``: never finishes its answer; it sends a byte now and then until the caller gives up.
- ``stand-in-failing``: answers 500.
- ``stand-in-tagger``: takes no prompt; one tag about the whole item, labelled with the task's first class.

It accepts any bearer token and refuses a call with none (401): who a token names is a real endpoint's to verify.

``python -m annotator.model_endpoint.stand_in`` serves it on ``MODEL_STAND_IN_HOST``:``MODEL_STAND_IN_PORT``
(127.0.0.1:8120); with ``--openapi`` it prints the protocol's OpenAPI document instead (`make model-protocol`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Annotated, Final

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from annotator.model_endpoint.protocol import (
    MODELS_PATH,
    PROPOSALS_PATH,
    ModelInfo,
    ModelListing,
    Point,
    Problem,
    Proposal,
    ProposalsRequest,
    ProposalsResponse,
)


VERSION: Final = "stand-in.1"

DETECTOR: Final = "stand-in-detector"
SEGMENTER: Final = "stand-in-segmenter"
SLOW: Final = "stand-in-slow"
FAILING: Final = "stand-in-failing"
TAGGER: Final = "stand-in-tagger"

MODELS: Final[tuple[ModelInfo, ...]] = (
    ModelInfo(name=DETECTOR, version=VERSION, title="Stand-in detector", reads=["image"], prompts=["text", "box"], returns=["bbox"]),
    ModelInfo(name=SEGMENTER, version=VERSION, title="Stand-in segmenter", reads=["image"], prompts=["box", "points"], returns=["polygon"]),
    ModelInfo(name=SLOW, version=VERSION, title="Stand-in that never answers", reads=["image"], prompts=["text", "box"], returns=["bbox"]),
    ModelInfo(name=FAILING, version=VERSION, title="Stand-in that fails", reads=["image"], prompts=["text", "box"], returns=["bbox"]),
    ModelInfo(name=TAGGER, version=VERSION, title="Stand-in tagger", reads=["image"], prompts=[], returns=["tag"]),
)

#: The box the detector proposes when the person drew none, and the side of the patch grown around a click.
DEFAULT_BOX: Final = (100.0, 100.0, 200.0, 80.0)
CLICK_PATCH: Final = 120.0
#: The detector's score, and the segmenter's with no click; each click raises the segmenter's by `CLICK_GAIN` up to
#: `SCORE_CEILING`, so a session that refines shows a score that moves.
DETECTOR_SCORE: Final = 0.7
TAGGER_SCORE: Final = 0.6
SEGMENTER_SCORE: Final = 0.75
CLICK_GAIN: Final = 0.05
SCORE_CEILING: Final = 0.95
#: How often the slow model sends a byte and looks whether its caller is still there.
SLOW_POLL_SECONDS: Final = 0.05

PROBLEM_JSON: Final = "application/problem+json"


def _problem(status: HTTPStatus, detail: str) -> JSONResponse:
    body = Problem(title=status.phrase, status=status.value, detail=detail)
    return JSONResponse(body.model_dump(), status_code=status.value, media_type=PROBLEM_JSON)


class _Refused(Exception):
    def __init__(self, status: HTTPStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


#: Declared so the published protocol says how a caller presents itself; refusing is `require_caller`'s, as a problem.
_BEARER: Final = HTTPBearer(auto_error=False, description="The caller's own token. An endpoint verifies whom it names.")


def require_caller(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]) -> None:
    """Refuse a call that presents no bearer token."""
    if credentials is None or not credentials.credentials.strip():
        raise _Refused(HTTPStatus.UNAUTHORIZED, "a call presents its caller's bearer token")


def _points_box(points: list[Point]) -> tuple[float, float, float, float]:
    """The patch a click session selects: its positive clicks' bounding box, padded by half a patch (every click when
    none is positive)."""
    anchors = [p for p in points if p.positive] or points
    xs, ys = [p.x for p in anchors], [p.y for p in anchors]
    pad = CLICK_PATCH / 2
    return (min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)


def _label(ask: ProposalsRequest) -> str:
    """The first class the task names, else the prompt's words, else nothing."""
    if ask.classes:
        return ask.classes[0]
    return (ask.prompts.text or "").strip()[:128]


def _detect(ask: ProposalsRequest) -> list[Proposal]:
    box = ask.prompts.box
    x, y, width, height = (box.x, box.y, box.width, box.height) if box is not None else DEFAULT_BOX
    return [Proposal(shape_type="bbox", x=x, y=y, width=width, height=height, label=_label(ask), score=DETECTOR_SCORE)]


def _segment(ask: ProposalsRequest) -> list[Proposal]:
    points, box = ask.prompts.points, ask.prompts.box
    if points:
        x, y, width, height = _points_box(points)
    elif box is not None:
        x, y, width, height = box.x, box.y, box.width, box.height
    else:
        raise _Refused(HTTPStatus.UNPROCESSABLE_ENTITY, f"{SEGMENTER} segments what a box or a click points at, and the ask carries neither")
    cx, cy = x + width / 2, y + height / 2
    score = min(SCORE_CEILING, SEGMENTER_SCORE + CLICK_GAIN * len(points))
    polygon = [cx, y, x + width, cy, cx, y + height, x, cy]
    return [Proposal(shape_type="polygon", x=x, y=y, width=width, height=height, polygon=polygon, label=_label(ask), score=score)]


def _tag(ask: ProposalsRequest) -> list[Proposal]:
    return [Proposal(shape_type="tag", label=_label(ask), score=TAGGER_SCORE)]


async def _drip_until_the_caller_leaves(request: Request) -> AsyncIterator[bytes]:
    """An answer that never ends: a byte of JSON whitespace now and then, for as long as the caller stays. A caller that
    bounds only each read waits for ever; one that bounds the whole ask gives up at its deadline."""
    while not await request.is_disconnected():
        yield b" "
        await asyncio.sleep(SLOW_POLL_SECONDS)


def create_stand_in() -> FastAPI:
    """The stand-in endpoint as an app."""
    app = FastAPI(title="Workbench model protocol", version="1", docs_url=None, redoc_url=None)

    @app.exception_handler(_Refused)
    async def refused(_request: Request, exc: _Refused) -> JSONResponse:
        return _problem(exc.status, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def outside_the_protocol(_request: Request, exc: RequestValidationError) -> JSONResponse:
        where = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()[:5])
        return _problem(HTTPStatus.UNPROCESSABLE_ENTITY, f"the ask is outside the protocol ({where})")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return _problem(HTTPStatus(exc.status_code), str(exc.detail))

    caller = [Depends(require_caller)]
    errors: dict[int | str, dict[str, object]] = {
        status.value: {"model": Problem, "description": status.phrase}
        for status in (
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.NOT_FOUND,
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    }

    @app.get(MODELS_PATH, dependencies=caller, responses={status: errors[status] for status in (401, 403, 429, 500, 503)})
    async def list_models() -> ModelListing:
        """The models this endpoint serves and what each can do."""
        return ModelListing(models=list(MODELS))

    @app.post(PROPOSALS_PATH, dependencies=caller, responses=errors, response_model=ProposalsResponse)
    async def propose(request: Request, ask: ProposalsRequest) -> ProposalsResponse | StreamingResponse:
        """One model's proposals on one item."""
        model = next((m for m in MODELS if m.name == ask.model), None)
        if model is None:
            raise _Refused(HTTPStatus.NOT_FOUND, f"this endpoint serves no model named {ask.model!r}")
        if ask.item.content.kind not in model.reads:
            raise _Refused(HTTPStatus.UNPROCESSABLE_ENTITY, f"{model.name} reads {', '.join(model.reads)}, not {ask.item.content.kind}")
        if unread := sorted(ask.prompts.kinds() - set(model.prompts)):
            raise _Refused(HTTPStatus.UNPROCESSABLE_ENTITY, f"{model.name} takes no {', '.join(unread)} prompt")
        if model.name == SLOW:
            return StreamingResponse(_drip_until_the_caller_leaves(request), media_type="application/json")
        if model.name == FAILING:
            raise _Refused(HTTPStatus.INTERNAL_SERVER_ERROR, f"{FAILING} fails every ask")
        answer = {SEGMENTER: _segment, TAGGER: _tag}.get(model.name, _detect)
        proposals = answer(ask)
        return ProposalsResponse(model=model.name, version=model.version, proposals=proposals)

    return app


def published_protocol() -> str:
    """The protocol as a machine reads it: this app's OpenAPI document, as `docs/model-protocol-openapi.json` holds it."""
    return json.dumps(create_stand_in().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Serve the stand-in, or with ``--openapi`` print the protocol's OpenAPI document."""
    if sys.argv[1:] == ["--openapi"]:
        sys.stdout.write(published_protocol())
        return
    import uvicorn

    uvicorn.run(create_stand_in(), host=os.environ.get("MODEL_STAND_IN_HOST", "127.0.0.1"), port=int(os.environ.get("MODEL_STAND_IN_PORT", "8120")))


if __name__ == "__main__":
    main()
