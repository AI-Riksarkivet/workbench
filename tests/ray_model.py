"""A Ray Serve deployment that speaks the workbench's model protocol, for the suite (ADR 0032).

It holds no model: its answers are fixed, as the stand-in's are, because what it proves is the Ray Serve path (Serve's
HTTP proxy, a route prefix, a full-size picture, a replica that is slow, busy or failing) and not a model. It checks who
calls as a deployment on a cluster must, since Serve verifies no token by itself (`docs/model-protocol.md` § A Ray Serve
deployment): each call's bearer token is signed by a key the issuer publishes (its key set read again when a token
names a key it does not hold, at most once a second), names that issuer, the audience the annotator's projected
ServiceAccount token is minted for, an expiry and an issue time, within 30 s of skew; anything else is refused 401.
A token that passes and names another subject than the annotator's ServiceAccount is refused 403: on a cluster any pod
may project a token for any audience, so the audience alone says nothing about who calls.

Ray's workers import this module by its name (`tests.ray_model`), so the suite starts Ray with the repository's root on
their `PYTHONPATH`.
"""

import asyncio
import base64
import hashlib
import time
from http import HTTPStatus
from typing import Final, cast

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ray import serve
from ray.serve import Application, Deployment

from annotator.model_endpoint.protocol import ImageContent, ModelInfo, ModelListing, Problem, Proposal, ProposalsRequest, ProposalsResponse


VERSION: Final = "ray.1"
DETECTOR: Final = "ray-detector"
SLOW: Final = "ray-slow"
BUSY: Final = "ray-busy"
FAILING: Final = "ray-failing"

MODELS: Final[tuple[ModelInfo, ...]] = tuple(
    ModelInfo(name=name, version=VERSION, title=title, reads=["image"], prompts=["text", "box"], returns=["bbox"])
    for name, title in (
        (DETECTOR, "Ray detector"),
        (SLOW, "Ray model that never answers"),
        (BUSY, "Ray model that is busy"),
        (FAILING, "Ray model that fails"),
    )
)

DETECTOR_SCORE: Final = 0.8
#: How long the slow model holds an ask: past any deadline a test gives the annotator.
SLOW_SECONDS: Final = 60.0
#: When the busy model tells its caller to ask again.
RETRY_AFTER_SECONDS: Final = 7
#: The clock skew a token's times are judged with, and how soon the key set is read again for a key it does not hold.
LEEWAY_SECONDS: Final = 30
KEY_REREAD_SECONDS: Final = 1.0
#: The claims a token must carry to be judged at all.
REQUIRED_CLAIMS: Final = ["exp", "iat", "iss", "aud", "sub"]

api = FastAPI(title="Ray Serve deployment speaking the workbench's model protocol")


class _Refused(Exception):
    def __init__(self, status: HTTPStatus, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


def _problem(status: HTTPStatus, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = Problem(title=status.phrase, status=status.value, detail=detail)
    return JSONResponse(body.model_dump(), status_code=status.value, media_type="application/problem+json", headers=headers)


@api.exception_handler(_Refused)
async def _refused(_request: Request, exc: _Refused) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return _problem(exc.status, exc.detail, headers)


@api.exception_handler(RequestValidationError)
async def _outside_the_protocol(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return _problem(HTTPStatus.UNPROCESSABLE_ENTITY, f"the ask is outside the protocol ({len(exc.errors())} error(s))")


@serve.deployment
@serve.ingress(api)
class ProtocolModels:
    """The protocol's two doors, behind Serve's HTTP proxy."""

    def __init__(self, jwks_url: str, issuer: str, audience: str, caller: str) -> None:
        self._jwks_url, self._issuer, self._audience, self._caller = jwks_url, issuer, audience, caller
        self._keys = self._read_keys()
        # When a token's unknown key last made the set be read again; the read at start does not count.
        self._reread_at = float("-inf")

    def _read_keys(self) -> jwt.PyJWKSet:
        return jwt.PyJWKSet.from_dict(httpx.get(self._jwks_url, timeout=10).json())

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        """The issuer's key ``kid``: from the set held, else from the set read again, which an issuer that rotated its
        signing key now publishes. Read again at most once a `KEY_REREAD_SECONDS`, so a stream of unknown keys costs the
        issuer little."""
        for held in (True, False):
            try:
                return self._keys[kid]
            except KeyError:
                if not held or time.monotonic() - self._reread_at < KEY_REREAD_SECONDS:
                    return None
                self._reread_at = time.monotonic()
                self._keys = await asyncio.to_thread(self._read_keys)
        return None

    async def _verify(self, request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _Refused(HTTPStatus.UNAUTHORIZED, "a call presents its caller's bearer token")
        refused = _Refused(HTTPStatus.UNAUTHORIZED, "this deployment does not accept the token presented")
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise refused from exc
        key = await self._key(kid) if isinstance(kid, str) else None
        if key is None:
            raise refused
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as exc:
            raise refused from exc
        if claims["sub"] != self._caller:
            raise _Refused(HTTPStatus.FORBIDDEN, "this caller may not ask this deployment's models")

    @api.get("/v1/models")
    async def models(self, request: Request) -> ModelListing:
        await self._verify(request)
        return ModelListing(models=list(MODELS))

    @api.post("/v1/proposals")
    async def proposals(self, request: Request, ask: ProposalsRequest) -> ProposalsResponse:
        await self._verify(request)
        if ask.model == SLOW:
            await asyncio.sleep(SLOW_SECONDS)
        if ask.model == BUSY:
            raise _Refused(HTTPStatus.SERVICE_UNAVAILABLE, f"{BUSY} is busy", retry_after=RETRY_AFTER_SECONDS)
        if ask.model == FAILING:
            raise _Refused(HTTPStatus.INTERNAL_SERVER_ERROR, f"{FAILING} fails every ask")
        if ask.model != DETECTOR:
            raise _Refused(HTTPStatus.NOT_FOUND, f"this deployment serves no model named {ask.model!r}")
        content = ask.item.content
        if not isinstance(content, ImageContent):
            raise _Refused(HTTPStatus.UNPROCESSABLE_ENTITY, f"{DETECTOR} reads image, not {content.kind}")
        # The proposal's text is the digest of the picture as this replica decoded it, so a test can tell the picture
        # arrived whole through the proxy.
        digest = hashlib.sha256(base64.b64decode(content.data, validate=True)).hexdigest()
        label = ask.classes[0] if ask.classes else ""
        proposal = Proposal(shape_type="bbox", x=10, y=20, width=30, height=40, label=label, text=digest, score=DETECTOR_SCORE)
        return ProposalsResponse(model=DETECTOR, version=VERSION, proposals=[proposal])


def application(jwks_url: str, issuer: str, audience: str, caller: str) -> Application:
    """The deployment bound to the issuer it trusts and the one subject it serves, ready for `serve.run`."""
    # `serve.deployment` over `serve.ingress` returns a Deployment, which the type checker cannot follow through the two.
    return cast("Deployment", ProtocolModels).bind(jwks_url, issuer, audience, caller)
