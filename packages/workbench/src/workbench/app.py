"""THE app factory: every workbench service is built by `make_service_app`, the media services (the viewer, search and
the annotator, through `workbench.media.app`) and flows alike.

It installs, once and identically for each: logging, the audit trail's switch, the exception handlers, the middleware
(request id, body cap, CORS), the probes, the SIGTERM drain, the request's trace and the export of its telemetry. What
is a service's own arrives as arguments: its settings (one shape, `workbench.config.Settings` or a subclass), its
routers, its lifespan, its readiness check and the response headers a browser may read.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI

from workbench.config import Settings
from workbench.context import CorrelationFilter, DiagnosticFormatter
from workbench.draining import arm_drain_on_sigterm
from workbench.exceptions import register_handlers
from workbench.governed.audit import configure_audit
from workbench.lifecycle import mark_draining, mark_started
from workbench.middleware import register_middleware
from workbench.ns_errors import install_problem_handlers
from workbench.otel import Telemetry, TelemetrySinks, setup_otel
from workbench.probes import ReadyCheck, make_probes_router
from workbench.slash import SlashToleranceMiddleware


def setup_logging(level: str) -> None:
    """Send application logs to stdout at ``level``, configured on the ROOT logger, so every module inherits it.

    Every module logs through ``logging.getLogger(__name__)``, so a handler on any narrower logger name would leave the
    records of every module outside it propagating to a root logger with no handler, and discarded: a best-effort
    branch that reports a failure with a WARNING would then report it nowhere.

    ``level`` is the volume lever (`Settings.log_level`, ``WORKBENCH_LOG_LEVEL``, default INFO). Root-level INFO makes
    chatty third-party libraries audible too; that is the trade against losing an application log, and one variable
    turns it down.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Idempotent by handler NAME, not by count: `make_service_app` runs this per app, and a duplicate
    # handler emits every line twice.
    if not any(h.get_name() == "workbench-stdout" for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("workbench-stdout")
        # THE REQUEST ID AND THE TRACE ID JOIN EVERY RECORD HERE, so an operator can grep stdout for the id a caller
        # quotes (`RequestIDMiddleware` echoes it) and for the trace id a dashboard shows.
        #
        # A FILTER on the one handler, not a `LoggingMiddleware`: a middleware annotates only the
        # records it writes itself, while this covers every module in every service — libraries
        # included — with no per-service edit. Outside a request it renders `-`, which is why the
        # context var defaults to a visible placeholder rather than an empty string that would read
        # like a formatting bug.
        #
        # The trace id comes from the filter too, not from `LoggingInstrumentor(set_logging_format=True)`: that reaches
        # `logging.basicConfig(format=...)`, which does nothing once a root handler exists, as this one does. So this
        # format string is also safe on a record that never met OpenTelemetry.
        handler.addFilter(CorrelationFilter())
        # `DiagnosticFormatter`, not `Formatter`: the correlation fields have slots in this string, and
        # everything the caller recorded in `extra=` follows the message. A format string cannot do the
        # second half — it names fixed fields, and the diagnostics differ at every call site.
        handler.setFormatter(DiagnosticFormatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] [%(trace_id)s] — %(message)s"))
        root.addHandler(handler)


def build_settings() -> Settings:
    """The settings the process's environment gives."""
    return Settings.model_validate({})


#: How long a stopping service waits for its telemetry to be sent. The chart gives the lifespan's teardown
#: `lifecycle.teardownSeconds` (10 s) after uvicorn's drain, inside the pod's grace period (40 s); this takes half of it,
#: leaving the rest to what the service's own teardown closes.
TELEMETRY_CLOSE_S: Final = 5.0


#: A service's own lifespan: what it opens before it serves and closes after, run inside the factory's.
type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _nothing_to_open(app: FastAPI) -> AsyncGenerator[None]:
    del app
    yield


def _factory_lifespan(settings: Settings, service_lifespan: Lifespan, installed: Sequence[Telemetry]) -> Lifespan:
    """The lifespan the factory gives every app: the service's own inside it, the lifecycle flags and the drain around
    it, and the telemetry in ``installed`` closed last.

    THE LIFECYCLE IS THE FACTORY'S, for every app: started once the service's own lifespan has opened what it serves
    with (never before, which would announce a half-built app), draining from SIGTERM (the drain hands the signal on to
    uvicorn) or from the lifespan's end, whichever comes first, and in a `finally`, so a lifespan that raises on the way
    out still leaves the process unready. What else `/readyz` asks is the service's `ready_check`, answered from the
    state of what it opened. ``installed`` is filled after the app exists, since the instrumentation wraps the app this
    lifespan belongs to.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.settings = settings
        try:
            async with service_lifespan(app):
                mark_started(app)
                disarm = arm_drain_on_sigterm(app)
                try:
                    yield
                finally:
                    mark_draining(app)
                    disarm()
        finally:
            # Last, after the service has closed what it opened, so what its teardown logged is sent too; off the event
            # loop, and within `TELEMETRY_CLOSE_S` whatever the gateway does.
            for telemetry_handle in installed:
                if not await asyncio.to_thread(telemetry_handle.close_within, TELEMETRY_CLOSE_S):
                    logging.getLogger(__name__).warning("telemetry_not_sent_at_shutdown", extra={"waited_s": TELEMETRY_CLOSE_S})

    return lifespan


def _mount_routes(app: FastAPI, *, prefix: str, routers: Sequence[APIRouter], root_routers: Sequence[APIRouter], ready_check: ReadyCheck | None) -> None:
    """Mount the probes and the service's routers, and resolve the other trailing-slash spelling in-process.

    THE OPERATIONAL PROBES are root-mounted for every app: one drain-aware `/livez` + `/readyz` pair. ROOT, not under
    ``prefix``, because a kubelet does not know a service's prefix. `ready_check` is the optional half, for a service
    that reports the state of what its lifespan opened. ``routers`` go under ``prefix``, ``root_routers`` at the paths
    they name themselves.
    """
    app.include_router(make_probes_router(ready_check))
    for router in routers:
        app.include_router(router, prefix=prefix)
    for router in root_routers:
        app.include_router(router)
    # Resolve the other trailing-slash spelling in-process instead of a
    # 307 redirect that would leak the service's in-pod address (see slash.py).
    app.router.redirect_slashes = False
    app.add_middleware(SlashToleranceMiddleware, routes_provider=lambda: app.router.routes)


def make_service_app(
    *,
    title: str,
    routers: Sequence[APIRouter] = (),
    root_routers: Sequence[APIRouter] = (),
    lifespan: Lifespan | None = None,
    ready_check: ReadyCheck | None = None,
    settings: Settings | None = None,
    expose_headers: Sequence[str] = (),
    telemetry: TelemetrySinks | None = None,
) -> FastAPI:
    """Build a service's FastAPI app.

    ``title`` names the service, in its OpenAPI document and as the ``service.name`` its telemetry is exported under.
    ``routers`` are mounted under ``settings.api_prefix``, ``root_routers`` at the root, at the paths they name
    themselves. ``lifespan`` is the service's own (what it opens and closes); the factory runs it inside its own, which
    puts ``settings`` on ``app.state.settings`` first. ``expose_headers`` are the response headers a named origin may read
    beside the request id. ``telemetry`` is where the telemetry goes when ``settings.otel_enabled``: the OTLP exporters
    unless a test names its own sinks.

    The layers a request meets, outermost first: the OpenTelemetry instrumentation (when telemetry is on), Starlette's
    server-error handler (the 500 for an exception nothing else handled), the trailing slash tolerance, the request id,
    the request line (one per response, `middleware.RequestLogMiddleware`), CORS, the body cap, then the domain exception
    handlers and the routers.

    `settings` IS INJECTABLE, and omitting it builds one from the environment: every caller builds its app at MODULE
    level, so the object passed is the one configuration the log level, the router prefix, the docs URLs, the
    middleware, `app.state.settings` and the telemetry all see.
    """
    settings = build_settings() if settings is None else settings
    setup_logging(settings.log_level)
    # THE AUDIT TRAIL IS ARMED HERE, for every app this factory builds (`Settings.audit_enabled`), so no service
    # inherits it from the log level instead of deciding it. Immediately after `setup_logging`, which sets the
    # root level this must override in both directions.
    configure_audit(enabled=settings.audit_enabled)
    installed: list[Telemetry] = []
    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=_factory_lifespan(settings, lifespan if lifespan is not None else _nothing_to_open, installed),
        # Opt-IN. See `Settings.docs_enabled` for why the default is closed rather than open.
        docs_url=f"{settings.api_prefix}/docs" if settings.docs_enabled else None,
        redoc_url=f"{settings.api_prefix}/redoc" if settings.docs_enabled else None,
        openapi_url=f"{settings.api_prefix}/openapi.json" if settings.docs_enabled else None,
    )
    register_handlers(app)
    # The rest of every service's answers: a validation error (422), a framework 404 or 405, an unexpected exception
    # (500, or 503 for a store that did not answer), and a lance-namespace error a door lets through, mapped by its
    # code. Installed after the domain handlers, so every error body a service answers comes from one of the two.
    install_problem_handlers(app, logging.getLogger(__name__))
    register_middleware(app, settings, expose_headers=expose_headers)
    _mount_routes(app, prefix=settings.api_prefix, routers=routers, root_routers=root_routers, ready_check=ready_check)
    telemetry_handle = setup_otel(app, service_name=title, settings=settings, sinks=telemetry)
    app.state.telemetry = telemetry_handle
    if telemetry_handle is not None:
        installed.append(telemetry_handle)
    return app
