"""THE app factory: every workbench service is built by `make_service_app`, the media services (the viewer, search and
the annotator, through `client_kit.media.app`) and flows alike.

It installs, once and identically for each: logging, the audit trail's switch, the exception handlers, the middleware
(request id, body cap, CORS), the probes, the SIGTERM drain, the request's trace and the export of its telemetry. What
is a service's own arrives as arguments: its settings (one shape, `service_kit.config.Settings` or a subclass), its
routers, its lifespan, its readiness check and the response headers a browser may read.

THIS IS A SUBMODULE, not the package `__init__`: as the `__init__`, importing ANYTHING under
`service_kit` — `service_kit.config` for a `Settings`, `service_kit.exceptions` for an error type —
would first execute this module and so import FastAPI, the OTel wiring, the middleware
stack and the probe router, and every consumer would pay for the app factory whether it wanted an app
or not. `service_kit/__init__.py` re-exports these names lazily, so `from service_kit import
make_service_app` resolves here.
"""

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI

from service_kit.config import Settings
from service_kit.context import CorrelationFilter, DiagnosticFormatter
from service_kit.draining import arm_drain_on_sigterm
from service_kit.exceptions import register_handlers
from service_kit.governed.audit import configure_audit
from service_kit.lifecycle import mark_draining, mark_started
from service_kit.middleware import register_middleware
from service_kit.otel import Telemetry, TelemetrySinks, setup_otel
from service_kit.probes import ReadyCheck, make_probes_router
from service_kit.slash import SlashToleranceMiddleware


def setup_logging() -> None:
    """Send application logs to stdout — configured on the ROOT logger, so every module inherits it.

    This used to attach the handler to two logger names, ``core`` and ``backends``, and its docstring
    said why: it "mirrors core.main". That monolith is gone. Every module in every service now uses
    ``logging.getLogger(__name__)`` — ``lineage.services.consumer``, ``maintenance.services.sweep``,
    ``catalog.api.v1.endpoints.tables`` — and NONE of those names sit under ``core.*`` or
    ``backends.*``. So the handler decorated two trees nobody logs to, every record propagated to a
    root logger with no handlers, and was discarded. Only uvicorn's own logs (which carry their own
    handlers) ever reached stdout, which is exactly why the estate looked quiet while it was not.

    Measured live 2026-08-17, and verified three times against the running service:

    * a malformed event POSTed to the running ingest answered ``{"status":"DROP"}`` — returned on the
      single line immediately AFTER ``log.error("lineage_event_invalid")``, so the branch demonstrably
      ran — and produced no log line at all. After this fix the identical request logs
      ``ERROR lineage.services.consumer — lineage_event_invalid``.

    The class this closes is the swallowed diagnostic — any handler that catches a failure by design
    and reports it with a WARNING instead. That warning had nowhere to go, so the one signal
    distinguishing "this is fine" from "this has been failing all week" did not exist. The estate has
    fewer of those than it did (lineage's feed write is now part of the ingest transaction and fails
    loudly rather than warning), but every best-effort arm that remains depends on this being fixed.

    ``WORKBENCH_LOG_LEVEL`` is the volume lever (default INFO). Root-level INFO does make chatty
    third-party libraries audible; that is the correct trade against losing every application log,
    and it is one env var to turn down rather than a hardcoded allowlist that goes stale the same way
    this one did.
    """
    level = os.environ.get("WORKBENCH_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent by handler NAME, not by count: `make_service_app` runs this per app, and a duplicate
    # handler emits every line twice.
    if not any(h.get_name() == "rask-stdout" for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("rask-stdout")
        # THE REQUEST ID JOINS EVERY RECORD HERE. `RequestIDMiddleware` mints one and echoes it to the
        # caller; until this filter existed nothing read it, so a user could quote an id from a failed
        # request and an operator had nothing to grep for.
        #
        # A FILTER on the one handler, not a `LoggingMiddleware`: a middleware annotates only the
        # records it writes itself, while this covers every module in every service — libraries
        # included — with no per-service edit. Outside a request it renders `-`, which is why the
        # context var defaults to a visible placeholder rather than an empty string that would read
        # like a formatting bug.
        handler.addFilter(CorrelationFilter())
        # THE TRACE ID JOINS IT, and this is the tier that was missing one. `LoggingInstrumentor`
        # attaches `otelTraceID` to every record, but the only way it ever reached a LINE was
        # `set_logging_format=True` → `logging.basicConfig(format=...)`, which no-ops because this
        # handler already exists. So the id rode on every record and printed on none of them: the
        # OTLP copy in GreptimeDB could be joined to a trace and `kubectl logs` — the copy an
        # operator reads first — could not. Both fields come from the filter, so this format string
        # is safe on a record that never met OpenTelemetry.
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


def _install_ns_problem_handlers(app: FastAPI) -> None:
    """Install the Lance-Namespace problem-detail translators, IF this deployable has them.

    THE IMPORT IS DEFERRED, and that is a packaging constraint rather than a performance one.
    `lance_namespace` lives behind service-kit's `[governed]` extra and MUST stay
    there: this library is shared by every service including the storeless ones, so a Lance dependency
    in its base is a Lance dependency in every one of them. Importing it at module scope makes
    `import service_kit` itself require the extra, and an image built without it fails at import —
    `ModuleNotFoundError: No module named 'lance_namespace'`, caught by `.docker/import-gate.py`
    while every test stays green (the workspace venv resolves the extra through a sibling member).

    SKIPPING IS SOUND, NOT A SILENT DEGRADATION, and this is the load-bearing argument. These handlers
    translate `lance_namespace` EXCEPTION TYPES. If the package is not importable then those classes do
    not exist in this process, so no code in it can raise one — and the governed kernel that raises them
    (`service_kit.governed.fga`) sits behind the very same extra, so it is absent too. There is nothing
    left for the handler to catch. A deployable that DOES serve that surface declares the extra and gets
    the handlers exactly as before.

    A missing `lance_namespace` is tolerated; anything else is re-raised. A broken import INSIDE
    `ns_errors` must not be swallowed as "the extra is absent" — that would turn a real defect into a
    quietly unhandled error class.
    """
    try:
        from service_kit.ns_errors import install_problem_handlers
    except ModuleNotFoundError as exc:
        if exc.name != "lance_namespace":
            raise
        logging.getLogger(__name__).debug(
            "lance_namespace absent — Lance-Namespace problem handlers not installed; nothing in this deployable can raise those types"
        )
        return
    install_problem_handlers(app, logging.getLogger(__name__))


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
    level, so the object passed is the one configuration the router prefix, the docs URLs, the middleware,
    `app.state.settings` and the telemetry all see.
    """
    setup_logging()
    # `build_settings()` is still the DEFAULT, not a fallback for a bad argument: `setup_logging()`
    # must run first either way, so a Settings validation failure is loggable rather than silent.
    settings = build_settings() if settings is None else settings
    # THE COMPLIANCE TRAIL IS ARMED HERE, for every app this factory builds — see
    # `Settings.audit_enabled`. Left to each service, seven of ten inherited it from `WORKBENCH_LOG_LEVEL`
    # instead of deciding it. Immediately after `setup_logging`, which sets the root level this must
    # override in both directions.
    configure_audit(enabled=settings.audit_enabled)
    base: Lifespan = lifespan if lifespan is not None else _nothing_to_open
    # Filled once the app exists: the instrumentation wraps the app the lifespan below belongs to.
    installed: list[Telemetry] = []

    @asynccontextmanager
    async def wrapped(app: FastAPI) -> AsyncGenerator[None]:
        # THE LIFECYCLE IS THE FACTORY'S, for every app: started once the service's own lifespan has opened what it
        # serves with (never before, which would announce a half-built app), draining from SIGTERM (the drain hands
        # the signal on to uvicorn) or from the lifespan's end, whichever comes first, and in a `finally`, so a
        # lifespan that raises on the way out still leaves the process unready. What else `/readyz` asks is the
        # service's `ready_check`, answered from the state of what it opened.
        app.state.settings = settings
        try:
            async with base(app):
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

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=wrapped,
        # Opt-IN. See `Settings.docs_enabled` for why the default is closed rather than open.
        docs_url=f"{settings.api_prefix}/docs" if settings.docs_enabled else None,
        redoc_url=f"{settings.api_prefix}/redoc" if settings.docs_enabled else None,
        openapi_url=f"{settings.api_prefix}/openapi.json" if settings.docs_enabled else None,
    )
    register_handlers(app)
    # AND the lance_namespace translator, because the governed kernel these apps call raises those
    # types: `governed/fga.py` raises `ServiceUnavailableError` on an FGA outage under a docstring
    # promising "outage → ServiceUnavailableError → 503". Without this the fleet apps could not map
    # one, so that outage answered a text/plain 500 — monitoring cannot tell a dependency being down
    # from this service being broken.
    #
    # Installed HERE rather than per app, so every service answers a denial as a 403 rather than a 500 and every
    # error body has one shape: the divergence docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md
    # "The Python estate audit" X11 files, one service's 422 body differing from its siblings'.
    _install_ns_problem_handlers(app)
    register_middleware(app, settings, expose_headers=expose_headers)

    # THE OPERATIONAL PROBES, root-mounted, for every app this factory builds.
    #
    # `service_kit.probes` exists so the estate has ONE drain-aware `/livez` + `/readyz` pair — its own
    # docstring records that the same twenty lines had already drifted three ways across six hand-rolled
    # copies. Left to each app, four of the five apps this factory built served neither probe at all
    # and one root-mounted it by hand; mounting it here makes the pair unconditional.
    #
    # ROOT, not under `api_prefix`: a kubelet does not know a service's prefix. Unconditional because
    # the router is dependency-free — `ready_check` is the optional half, for a service with a hard
    # dependency worth reporting (a state store, say).
    #
    # `/api/health` is untouched: it is the frontend-facing badge the estate documents it to be. The
    # point is that it is not a READINESS answer — it is a constant, so probing it reports "ready"
    # while the pod drains.
    app.include_router(make_probes_router(ready_check))

    for router in routers:
        app.include_router(router, prefix=settings.api_prefix)
    for router in root_routers:
        app.include_router(router)

    # Resolve the other trailing-slash spelling in-process instead of a
    # 307 redirect that would leak the service's in-pod address (see slash.py).
    app.router.redirect_slashes = False
    app.add_middleware(SlashToleranceMiddleware, routes_provider=lambda: app.router.routes)

    telemetry_handle = setup_otel(app, service_name=title, settings=settings, sinks=telemetry)
    app.state.telemetry = telemetry_handle
    if telemetry_handle is not None:
        installed.append(telemetry_handle)

    return app
