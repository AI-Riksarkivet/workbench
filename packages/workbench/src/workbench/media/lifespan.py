"""THE media-service lifespan — one implementation for every service built on the media library.

They are one shape wearing several names: a Lance media service over ``workbench.media``, built by the kit's factory
(`workbench.app.make_service_app`), which runs this lifespan inside its own: the factory marks the app started once
this has opened what the service serves with, arms the SIGTERM drain, and marks it draining before this closes anything.

WHAT IS GENUINELY PER-SERVICE stays per-service, and is passed in rather than branched on here: the state the service
serves from (``make_state``), which resources it opened and must therefore close (``closes``), and any extra startup a
service needs before it may report ready (``setup``) with its matching teardown. The annotator's state store is the
case that hook serves: a pod that reports ready before its store is open is a pod answering 503 to every route that
needs it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI

from workbench.governed.auth_lifespan import _AuthSettings, attach_auth
from workbench.governed.auth_lifespan import dispose as auth_dispose
from workbench.media.config import MediaSettings
from workbench.media.state import AppState


log = logging.getLogger(__name__)

#: Extra startup for one service, run after `attach_auth`, before the service may report ready.
type MediaSetup = Callable[[FastAPI, AppState], Awaitable[None]]
#: Its matching teardown, run first in the `finally` — before anything shared is disposed.
type MediaTeardown = Callable[[FastAPI], Awaitable[None]]
#: The resources THIS service opened, named per service because `AppState` carries slots for all
#: three (the search service's model handles among them) and closing a slot you did not fill is a
#: no-op that reads as ownership.
type MediaCloses = Callable[[AppState], Sequence[Any]]
#: How a service builds the state it serves from, out of its settings.
type MediaStateFactory = Callable[[MediaSettings], AppState]


def make_media_state(settings: MediaSettings) -> AppState:
    """The media plane's `AppState`: its dataset registry, and its one process HTTP pool."""
    return AppState(settings=settings, http=httpx.Client())


@asynccontextmanager
async def media_lifespan(
    app: FastAPI,
    settings: MediaSettings,
    *,
    service: str,
    closes: MediaCloses,
    make_state: MediaStateFactory = make_media_state,
    setup: MediaSetup | None = None,
    teardown: MediaTeardown | None = None,
) -> AsyncGenerator[AppState]:
    """Run one media service's whole lifecycle. Yields the ``AppState`` it built.

    Startup, in order:

    1. Build ``AppState`` onto ``app.state.resources`` (its registry with it) — the shape every media router reads
       through ``StateDep``.
    2. ``attach_auth``: the verifier and the authorization client the settings name. Built here, never at import — an ``OIDCVerifier`` fetches discovery, so module scope
       would make importing a main do I/O. A failure to BUILD is logged and non-fatal: the dependency
       then finds no verifier/client on ``app.state`` and answers 503, and the readiness check
       (`workbench.media.app.media_readiness`) reports the pod unready. Falling back to a permissive checker would
       turn a broken authorization layer into an open one.
    3. The service's own ``setup``, if any.

    Teardown runs in a ``finally``, so an exception past the ``yield`` cannot skip it.
    """
    state = make_state(settings)
    app.state.resources = state
    # CAST, not a widened parameter: `attach_auth` wants the structural `_AuthSettings` (issuer, audience, idp id), and
    # every settings object that reaches here is a `MediaSettings` ALSO mixing in `OidcSettings` — `ViewerSettings`,
    # `SearchSettings` and `AnnotatorSettings` do. Python has no intersection type to say that, and typing the parameter
    # as the protocol instead would lose the `MediaSettings` that `AppState` requires.
    await attach_auth(app, cast(_AuthSettings, settings), service=service)
    try:
        if setup is not None:
            await setup(app, state)
        yield state
    finally:
        if teardown is not None:
            await teardown(app)
        # What `attach_auth` opened: the OpenFGA client (aiohttp-backed, so collected unclosed it leaves a half-open
        # connection per replica) and the token client's HTTP pool. Disposal lives beside the builder.
        await auth_dispose(app)
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
    make_state: MediaStateFactory = make_media_state,
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
        async with media_lifespan(app, settings_factory(), service=service, closes=closes, make_state=make_state, setup=setup, teardown=teardown):
            yield

    return lifespan
