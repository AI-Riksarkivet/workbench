"""The ONE operational-probe router: ``/livez`` (dependency-free) + ``/readyz`` (state-gated).

Separate from a service's ``/api/health`` (whose body is a service-specific contract). These two are for a process
supervisor / load balancer: livez proves the event loop is responsive, readyz reports lifecycle gating off
``app.state`` plus — optionally — one service-specific check. One home, one shape (drain first, then startup, then the
check; the ``Readiness`` body), and ``async def``: a sync handler would put liveness on the blocking threadpool, where
it queues behind heavy Arrow/Lance work and fails exactly when the pod is busiest.

``workbench.app.make_service_app`` mounts :func:`make_probes_router` on every app, with the service's ``ready_check``
when it has a fact worth reporting (a media service's components, the annotator's state store).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from workbench.lifecycle import is_draining, is_started
from workbench.schemas.health import Liveness, Readiness, ReadinessStatus


#: A service-specific readiness check, run only AFTER the lifecycle gates pass. Returns the
#: ``Readiness`` body to serve: ``status=ready`` → 200, anything else (``degraded``) → 503.
type ReadyCheck = Callable[[Request], Awaitable[Readiness]]


def _serve(body: Readiness) -> JSONResponse:
    status = HTTPStatus.OK if body.status is ReadinessStatus.ready else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def make_probes_router(ready_check: ReadyCheck | None = None) -> APIRouter:
    """Build the probe router, optionally with a service-specific readiness check."""
    probes = APIRouter(tags=["health"])

    @probes.get("/livez")
    async def livez() -> Liveness:
        # async, not sync def: the probe must run ON the event loop, never queued behind the
        # blocking data-plane threadpool — no I/O happens here, so there is nothing to yield for.
        return Liveness()

    @probes.get("/readyz", response_model=Readiness)
    async def readyz(request: Request) -> JSONResponse:
        state = request.app.state
        # Drain first: once the lifespan flips the drain flag, that is the answer regardless of
        # anything else, and the check must never touch a dependency that is already closing.
        #
        # Both reads go through `workbench.lifecycle`, which is also what sets them, so the flags are
        # one typed seam between this handler and the factory's lifespan.
        if is_draining(state):
            return _serve(Readiness(status=ReadinessStatus.shutting_down))
        if not is_started(state):
            return _serve(Readiness(status=ReadinessStatus.starting))
        if ready_check is None:
            return _serve(Readiness(status=ReadinessStatus.ready))
        return _serve(await ready_check(request))

    return probes
