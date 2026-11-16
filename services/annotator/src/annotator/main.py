"""Annotator service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). The lifespan and the assembly are both SHARED — ``client_kit.media.lifespan`` and
``client_kit.media.app`` — because viewer, search and annotator are three deployments of one shape.
What is genuinely this service's own — its state store and its publish runner — arrives through the shared lifespan's
``setup`` hook, which runs before readiness is announced, and leaves through its ``teardown``.

The annotator is a client of the lakehouse with its own release (ADR 0171): it publishes nothing on the
lakehouse's bus, and tells a person about their work on its own surface.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request

from annotator.api.dependencies import NO_STATE_STORE
from annotator.api.v1.router import router as api_router
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.projects.lakehouse import PublishRunner
from annotator.state.store import AnnotatorStore
from client_kit.media.app import build_media_app
from client_kit.media.config import MediaSettings
from client_kit.media.lifespan import make_media_lifespan
from client_kit.media.state import AppState
from service_kit import setup_logging
from service_kit.obs import configure_app_logging
from service_kit.schemas.health import Readiness, ReadinessStatus


logger = logging.getLogger(__name__)

configure_app_logging(packages=("annotator", "client_kit"))  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


async def _open_state(app: FastAPI, state: AppState) -> None:
    """The annotator's own startup, run BEFORE readiness is announced: its state store and its publish runner.

    **No state database named, no project and task plane:** the store stays unset, every route of that plane answers
    503 naming why (`annotator.api.dependencies.get_store`), and the read plane serves.

    **A state database named and unreachable fails the start.** The database is the service's own, declared beside it,
    so a start that cannot reach it is a pod the platform restarts until it can, rather than one reporting ready while
    every project route fails.

    The publish runner's re-drive starts here: it claims every publish whose run stopped, in this or any replica, at
    once and then about every `ANNOTATOR_PUBLISH_REDRIVE_SECONDS`.
    """
    settings = state.settings
    if not isinstance(settings, AnnotatorSettings):
        raise TypeError(f"the annotator's lifespan needs AnnotatorSettings, not {type(settings).__name__}")
    app.state.store = None
    app.state.publish_runner = None
    if settings.state_host is None:
        logger.warning("annotator: %s; the project and task plane will 503", NO_STATE_STORE)
        return
    store = await AnnotatorStore.open(settings)
    runner = PublishRunner(
        store,
        getattr(app.state, "lakehouse_tokens", None),
        lease_seconds=settings.publish_lease_seconds,
        redrive_seconds=settings.publish_redrive_seconds,
        concurrency=settings.publish_concurrency,
    )
    app.state.store, app.state.publish_runner = store, runner
    runner.start()


async def _close_state(app: FastAPI) -> None:
    runner: PublishRunner | None = getattr(app.state, "publish_runner", None)
    if runner is not None:
        await runner.close()
    store: AnnotatorStore | None = getattr(app.state, "store", None)
    if store is not None:
        await store.close()


# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()


async def _state_ready(request: Request) -> Readiness:
    """Report the state store as a COMPONENT of a 200, never as `degraded`.

    `service_kit.probes` renders `degraded` as a 503, which would pull the pod from rotation and take the read-plane
    annotation routes down with the project plane. A store that was named opened before readiness, or the start
    failed; one that was not named is reported as such.
    """
    store = getattr(request.app.state, "store", None)
    return Readiness(status=ReadinessStatus.ready, components={"state": "open" if store is not None else "unconfigured"})


def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_annotator_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_annotator_settings()


#: `closes` names the resources the shared state holds for this service: only the http pool. The state store and the
#: publish runner are this service's own and close in `_close_state`.
lifespan = make_media_lifespan(
    _settings,
    service="annotator",
    closes=lambda state: (state.http,),
    setup=_open_state,
    teardown=_close_state,
)

app = build_media_app(
    title="lance-media annotator",
    settings=get_annotator_settings(),
    routers=[api_router],
    lifespan=lifespan,
    ready_check=_state_ready,
)


def run() -> None:
    import uvicorn

    s = get_annotator_settings()
    uvicorn.run("annotator.main:app", host=s.host, port=s.service_port)
