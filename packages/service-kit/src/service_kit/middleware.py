"""HTTP middleware — cross-cutting request concerns.

ORDER MATTERS, AND IT IS INVERTED. Starlette inserts each `add_middleware(...)` at the FRONT of the
stack, so the LAST call wraps everything else and is the first to see a request and the last to touch
a response. Registration order is therefore innermost-first, which reads backwards and is exactly how
CORS ended up in the wrong place.

Outermost → innermost, i.e. the order a request meets them (`make_service_app` adds the slash tolerance last, so it is
outside all of these; the OpenTelemetry instrumentation, when on, wraps the whole stack):

  1. RequestID    — sets `request.state.request_id` + the context var, echoes `X-Request-ID` on every response
  2. RequestLog   — writes the one line every response leaves
  3. CORS         — closed unless `settings.cors_origins` names an origin
  4. BodySizeLimit— rejects an over-cap body as the bytes arrive

THE REQUEST LINE IS OUTSIDE EVERY LAYER THAT ANSWERS: uvicorn's access log is off for every service, since it prints the
path and query, and a path can carry a credential (a signed media URL's token) and a query a person's search. So a
refusal by CORS (a preflight from an origin not named) or by the body cap (a 413) is recorded too; the request id is
outside it, so its line carries the id.

CORS IS OUTSIDE THE BODY CAP BECAUSE A REJECTION IS A RESPONSE TOO: the body-limit 413, the one a browser upload hits,
carries the headers that let a named origin read it, instead of reaching the browser as an opaque network error.

NO LAYER MAY BE `BaseHTTPMiddleware`-BASED. It buffers the whole response body, which breaks a streamed
response (the viewer's 206 Range responses, which video seeking depends on). Each layer here is pure ASGI:
it rewrites only the response START message, or refuses before the body is read.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from typing import Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.config import Settings
from service_kit.context import request_id_ctx
from service_kit.exceptions import scope_route_template


_REQUEST_ID_HEADER = "X-Request-ID"
#: The probes a kubelet asks every few seconds: a line for each would bury the requests people make.
_UNLOGGED_PATHS: Final = frozenset({"/livez", "/readyz"})
_requests = logging.getLogger("service_kit.requests")


class RequestLogMiddleware:
    """One INFO line per response, on `service_kit.requests`: its method, its route's TEMPLATE
    (`/api/signed-media/{token}`, or `(no route)`), its status, whether the app finished it (`complete`), its duration
    and its request id (the line's own field), never its path or query. Written whether telemetry is on or off, so every
    service keeps a record of what it sent that carries no secret.

    A handler that raises before it answers is recorded with the 500 the server answers it with, `complete` false; one
    that raises after its response started (a stream cut short) with the status it had sent, `complete` false. Pure ASGI:
    it reads the response start message and passes every body chunk on untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _UNLOGGED_PATHS:
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        complete = False

        async def _send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, _send)
            complete = True
        finally:
            # The route the outer layers read is the one the request matched as mounted: FastAPI (0.140) leaves an included
            # router's own route in `scope["route"]`, without its prefixes, which the telemetry's duration metric would
            # name (`http.target`) beside a span naming the prefixed template.
            mounted = (scope.get("fastapi") or {}).get("effective_route_context")
            if mounted is not None:
                scope["route"] = mounted
            # The router writes the route it matched into this same scope; none matched is said so.
            _requests.info(
                "request",
                extra={
                    "method": scope.get("method", ""),
                    "route": scope_route_template(scope) or "(no route)",
                    "status": status,
                    "complete": complete,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "request_id": request_id_ctx.get(),
                },
            )


class RequestIDMiddleware:
    """Echo or generate `X-Request-ID` per request; publish it to `request.state` and the context var.

    Inbound `X-Request-ID` is preserved so a reverse proxy / client can correlate logs across hops.
    Without it, we mint a UUID hex.

    PURE ASGI, NOT `BaseHTTPMiddleware`, and that is what makes it safe everywhere. `BaseHTTPMiddleware`
    fully buffers the response body, which would break a streamed body such as a 206 Range response; the
    catalog's Arrow-IPC data plane has the same shape. Because this one rewrites only the response START
    message and passes the body through untouched, no streaming route needs an exemption and every
    service can carry one id.

    It touches only the response START message to add the header; body chunks pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get(_REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        # `scope["state"]` is what `request.state` reads, so existing `request.state.request_id`
        # consumers keep working without knowing this middleware changed shape.
        scope.setdefault("state", {})["request_id"] = request_id

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                raw.append((_REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        # PUBLISHED TO THE CONTEXT, so code that does not take `request` — a log record, a repository
        # method, a background helper — can read it without plumbing. Before this the id was minted,
        # echoed and read by nothing: a caller could quote it and an operator had nothing to grep.
        token = request_id_ctx.set(request_id)
        try:
            await self.app(scope, receive, _send)
        finally:
            # THE RESET IS THE LOAD-BEARING HALF. Without it the value survives the response on a
            # reused worker and labels the NEXT request with the previous caller's id — worse than no
            # id at all, because it correlates the wrong things while looking correct.
            request_id_ctx.reset(token)


def register_middleware(app: FastAPI, settings: Settings, *, expose_headers: Sequence[str] = ()) -> None:
    """Register all HTTP middleware on the app: the request id, the request line, CORS and the body ceiling, in that
    order from the outside in.

    Called once from `make_service_app` after `register_handlers()`. The call order here is
    INNERMOST-FIRST — see the module docstring for the order a request actually meets. ``expose_headers`` are the
    response headers a named origin's script may read beside `X-Request-ID` (the media services' Range headers).
    """
    # THE BODY CEILING. Pure-ASGI, so the cap applies as bytes ARRIVE — before the body is buffered (a multipart
    # file part is spooled whole before the handler runs) and independent of how the endpoint reads it.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    # Outside the body cap, so its 413 carries the headers a named origin needs to read it. Installed with no origin
    # named too, so a cross-origin preflight is refused by CORS itself: every app is reached same-origin through the
    # Gateway. No credentials: every caller presents a bearer or DPoP header, never a cookie.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[_REQUEST_ID_HEADER, *expose_headers],
    )
    # Outside every layer that answers, so each response leaves its line; inside the request id, which it carries.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
