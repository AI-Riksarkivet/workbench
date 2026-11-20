"""THE media-service lifespan — one implementation for every service built on the media library.

They are one shape wearing several names: a Lance media service over ``client_kit.media``. Each
one hand-wrote the same startup and the same teardown, and docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-16 is that copy.
The copies had already drifted in the way copies do — not in what they did, but in the ORDER they did
it:

* viewer disposed the OpenFGA client, THEN disarmed the SIGTERM drain;
* search disarmed the drain, THEN disposed.

Neither order is wrong on its own, and that is exactly why the copies could hold different ones:
nothing compared them. One implementation makes the order a decision instead of an accident.

WHAT IS GENUINELY PER-SERVICE stays per-service, and is passed in rather than branched on here:
which resources this service opened and must therefore close (``closes``), and any extra startup a
service needs before it announces readiness (``setup``) with its matching teardown. The annotator's state
store is the case that hook serves, and it must run BEFORE ``startup_complete`` — a pod that reports ready
before its store is open is a pod answering 503 to every route that needs it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI

from client_kit.media.config import MediaSettings
from client_kit.media.state import AppState
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.auth_lifespan import _AuthSettings, attach_auth
from service_kit.governed.auth_lifespan import dispose as auth_dispose


log = logging.getLogger(__name__)

#: Extra startup for one service, run after `attach_auth` and BEFORE the readiness flags.
type MediaSetup = Callable[[FastAPI, AppState], Awaitable[None]]
#: Its matching teardown, run first in the `finally` — before anything shared is disposed.
type MediaTeardown = Callable[[FastAPI], Awaitable[None]]
#: The resources THIS service opened, named per service because `AppState` carries slots for all
#: three (the search service's model handles among them) and closing a slot you did not fill is a
#: no-op that reads as ownership.
type MediaCloses = Callable[[AppState], Sequence[Any]]


def make_media_state(settings: MediaSettings) -> AppState:
    """The media plane's `AppState`, with its one process HTTP pool.

    One line, and it was written in four places — the three lifespans plus `viewer.create_viewer_state`
    — which is enough for it to have been the thing DUP-16's grep found. It lives beside the lifespan
    that uses it so "what a media service opens at boot" has one answer.
    """
    return AppState(settings=settings, http=httpx.Client())


@asynccontextmanager
async def media_lifespan(
    app: FastAPI,
    settings: MediaSettings,
    *,
    service: str,
    closes: MediaCloses,
    setup: MediaSetup | None = None,
    teardown: MediaTeardown | None = None,
) -> AsyncGenerator[AppState]:
    """Run one media service's whole lifecycle. Yields the ``AppState`` it built.

    Startup, in order:

    1. Build ``AppState`` onto ``app.state.resources`` — the shape every media router reads through
       ``StateDep``.
    2. ``attach_auth``. Every part defaults OFF and the service behaves exactly as it always did when
       they are. Built here, never at import — an ``OIDCVerifier`` fetches discovery, so module scope
       would make importing a main do I/O. A failure to BUILD is logged and non-fatal: the dependency
       then finds no verifier/client on ``app.state`` and answers 503, which is the honest reading.
       Falling back to a permissive checker would turn a broken authorization layer into an open one.
    3. The service's own ``setup``, if any — before readiness is announced.
    4. The lifecycle flags ``/readyz`` reads, then the SIGTERM drain.

    THE DRAIN IS ARMED AT SIGTERM, not at lifespan shutdown: the ``shutting_down`` flag below flips in
    the ``finally``, which uvicorn only reaches AFTER it has stopped accepting connections and
    drained — so the admission guards that read it refused nothing, ever. Kubernetes sends SIGTERM at
    the START of termination, while the Gateway may still be sending this pod requests. (Owner ruling
    2026-08-25.)

    Teardown runs in a ``finally``, so an exception past the ``yield`` cannot skip it — the bare
    ``yield`` this replaces leaked the http client, the embedder and the reranker on exactly the path
    where leaking matters.
    """
    state = make_media_state(settings)
    app.state.resources = state
    # CAST, not a widened parameter: `attach_auth` wants the structural `_AuthSettings` (issuer, audience, idp id), and
    # every settings object that reaches here is a `MediaSettings` ALSO mixing in `OidcSettings` — `ViewerSettings`,
    # `SearchSettings` and `AnnotatorSettings` do. Python has no intersection type to say that, and typing the parameter
    # as the protocol instead would lose the `MediaSettings` that `AppState` requires.
    await attach_auth(app, cast(_AuthSettings, settings), service=service)
    if setup is not None:
        await setup(app, state)
    app.state.startup_complete = True
    app.state.shutting_down = False
    _disarm_drain = arm_drain_on_sigterm(app)
    try:
        yield state
    finally:
        if teardown is not None:
            await teardown(app)
        # What `attach_auth` opened: the OpenFGA client (aiohttp-backed, so collected unclosed it leaves a half-open
        # connection per replica) and the token client's HTTP pool. Disposal lives beside the builder.
        await auth_dispose(app)
        _disarm_drain()
        app.state.shutting_down = True
        for resource in closes(state):
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    log.warning("error closing %s on shutdown", type(resource).__name__)


def make_media_lifespan(
    settings_factory: Callable[[], MediaSettings],
    *,
    service: str,
    closes: MediaCloses,
    setup: MediaSetup | None = None,
    teardown: MediaTeardown | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """The lifespan as FastAPI wants it: a one-argument factory over ``app``.

    ``settings_factory`` rather than a settings OBJECT because a main is imported long before it is
    run — resolving the settings at import would make the module's import order decide the process's
    configuration, which is the trap ``AppState``'s own per-state settings note records.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with media_lifespan(app, settings_factory(), service=service, closes=closes, setup=setup, teardown=teardown):
            yield

    return lifespan
