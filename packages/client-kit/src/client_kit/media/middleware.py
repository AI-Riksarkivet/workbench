"""The MEDIA plane's HTTP middleware stack: one request id, the request-body ceiling, and CORS outermost.

NO LAYER ON THIS PLANE MAY BE ``BaseHTTPMiddleware``-BASED — a constraint on anything added later, not
a description of what happens to be here. ``BaseHTTPMiddleware`` fully buffers the response body,
which breaks the ``/api/explorer`` ``StreamingResponse`` Range streaming that 206 partial-content
video seeking depends on. Each layer below is pure ASGI: it rewrites only the response START message,
or refuses before the body is read, so a streaming response passes through chunk for chunk. A timing
or auth layer wanted here is written the same way.

``expose_headers`` is load-bearing: the browser needs Content-Range /
Content-Length / Accept-Ranges visible to seek video.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from client_kit.media.config import Settings
from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.middleware import RequestIDMiddleware


def register_media_middleware(app: FastAPI, settings: Settings) -> None:
    """Register the request id, the request-body ceiling and CORS for a MEDIA app. Called once from ``build_media_app``.

    NAMED FOR ITS PLANE (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-20).
    `service_kit.middleware.register_middleware` is a DIFFERENT stack (it adds Timing and sends `allow_credentials`);
    this one exposes the Range headers a browser needs to seek video and runs no Timing layer.

    Starlette runs the layer added LAST first, so CORS, added last, is the outermost: a response another layer answers
    itself (the body ceiling's 413) carries the headers that let a named origin read it."""
    # ONE ID PER REQUEST, on this plane too: `RequestIDMiddleware` is pure ASGI and rewrites only the
    # response START message, never a body chunk, so the media trio gets the same correlation id every
    # other service has and the Range streaming below is untouched.
    app.add_middleware(RequestIDMiddleware)
    # THE BODY CEILING, and these are the apps that most needed one: the media apps are where multipart
    # uploads arrive. A file part is spooled whole to disk BEFORE the handler runs, so
    # a handler-side `read(cap + 1)` bounds that handler's memory and nothing about the landing zone.
    # Pure-ASGI, so this refuses as bytes arrive — before starlette spools anything.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        # Closed unless the deployment names an origin: the app calls every service same-origin through the Gateway.
        allow_origins=settings.cors_origins,
        # A media app may serve PUT/PATCH/DELETE; with the write verbs absent, a cross-origin browser preflight for them is answered without them in
        # Access-Control-Allow-Methods and the real request is blocked. These are the methods the
        # media apps actually route.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
    )
