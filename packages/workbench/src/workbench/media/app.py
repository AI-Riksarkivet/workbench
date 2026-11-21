"""What a media service (the viewer, search, the annotator) adds to the kit's app factory, and nothing else.

Every service is built by `workbench.app.make_service_app`, which installs the exception handlers, the request id, the
body cap, CORS, the probes, the drain and the telemetry once and identically. A media service composes onto it:

* its routers at the ROOT, at the ``/api/...`` paths they name themselves (the paths the app calls under each service's
  public prefix);
* the Range headers a browser must read to seek a video, among the headers a named origin may read;
* its readiness, answered from what its lifespan opened (:func:`media_readiness`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI, Request

from workbench.app import make_service_app
from workbench.media.config import MediaSettings
from workbench.media.state import AppState
from workbench.otel import TelemetrySinks
from workbench.probes import ReadyCheck
from workbench.schemas.health import Readiness, ReadinessStatus


#: The response headers a browser must read to seek a video (`Content-Range`, `Accept-Ranges`) and to size a body.
MEDIA_EXPOSE_HEADERS = ("Content-Range", "Content-Length", "Accept-Ranges")

#: How a component reads in `/readyz`: ready, off by the deployment's choice, or not (yet) able to serve.
READY, OFF = "ready", "off"


def _components(request: Request) -> dict[str, str]:
    """Each thing a media service serves with, as its lifespan left it: the state and its dataset registry, the sign-in
    verifier when sign-in is on, and the workbench's OpenFGA client when authorization is on."""
    app_state = request.app.state
    state: AppState | None = getattr(app_state, "resources", None)
    if state is None:
        return {"registry": "not opened"}
    settings = state.settings
    sign_in = READY if getattr(app_state, "oidc", None) is not None else "no verifier: every signed-in request answers 503"
    authz = READY if getattr(app_state, "authz", None) is not None else "store not found yet: every authorized request answers 503"
    return {
        "registry": READY,
        "sign_in": sign_in if getattr(settings, "oidc_enabled", False) else OFF,
        "authz": authz if getattr(settings, "fga_enabled", False) else OFF,
    }


def media_readiness(extra: ReadyCheck | None = None) -> ReadyCheck:
    """`/readyz` for a media service: ready only while every component is ready or off, and ``extra`` (a service's own
    check) is ready too. A component that is not is a 503 that takes the pod out of rotation; liveness never asks."""

    async def ready(request: Request) -> Readiness:
        components = _components(request)
        status = ReadinessStatus.ready if all(value in (READY, OFF) for value in components.values()) else ReadinessStatus.degraded
        if extra is not None:
            own = await extra(request)
            components |= own.components
            if own.status is not ReadinessStatus.ready:
                status = own.status
        return Readiness(status=status, components=components)

    return ready


def build_media_app(
    *,
    title: str,
    settings: MediaSettings,
    routers: Sequence[APIRouter],
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    ready_check: ReadyCheck | None = None,
    telemetry: TelemetrySinks | None = None,
) -> FastAPI:
    """One media service's app, from the kit's factory: ``title`` is the service's name (``viewer``, ``search``,
    ``annotator``), as its telemetry is exported under it. ``ready_check`` is the service's own part of readiness, beside
    the components every media service has. ``lifespan`` is optional so the test/composition seam can build the SAME app
    around state it opened itself."""
    return make_service_app(
        title=title,
        settings=settings,
        root_routers=routers,
        lifespan=lifespan,
        ready_check=media_readiness(ready_check),
        expose_headers=MEDIA_EXPOSE_HEADERS,
        telemetry=telemetry,
    )
