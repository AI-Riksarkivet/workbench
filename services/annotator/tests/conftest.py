"""The annotator's state for its offline tests: a real PostgreSQL database of the test's own.

`state_settings` names the test's database (root `conftest.py::state_database`) in the annotator's settings, the way a
deployment's environment does, so the service's own lifespan (`annotator.main`) opens its store on it. `state_app`
composes a test app from routers with the same store and publish runner, opened in the app's own lifespan: psycopg's
async pool lives on the event loop that opened it, so a test seeds rows through that loop
(`client.portal.call(client.app.state.store.<method>, ...)`), never through one of its own.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi import APIRouter, FastAPI

from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.projects.lakehouse import PublishRunner
from annotator.state.store import AnnotatorStore
from service_kit.exceptions import register_handlers
from tests.postgres import StateDatabase


@pytest.fixture
def state_settings(state_database: StateDatabase, monkeypatch: pytest.MonkeyPatch) -> Iterator[AnnotatorSettings]:
    """The annotator's settings naming this test's database, as the environment a deployment renders."""
    for name, value in state_database.env().items():
        monkeypatch.setenv(name, value)
    get_annotator_settings.cache_clear()
    yield get_annotator_settings()
    get_annotator_settings.cache_clear()


@pytest.fixture
def state_app(state_settings: AnnotatorSettings) -> Callable[..., FastAPI]:
    """Build a test app of the given routers whose lifespan opens the state store and the publish runner."""

    def build(*routers: APIRouter) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
            store = await AnnotatorStore.open(state_settings)
            # No re-drive started: a test's publish runs only when its door starts it.
            runner = PublishRunner(
                store,
                None,
                lease_seconds=state_settings.publish_lease_seconds,
                redrive_seconds=state_settings.publish_redrive_seconds,
                concurrency=state_settings.publish_concurrency,
            )
            app.state.store, app.state.publish_runner = store, runner
            try:
                yield
            finally:
                await runner.close()
                await store.close()

        app = FastAPI(lifespan=lifespan)
        register_handlers(app)
        for router in routers:
            app.include_router(router)
        return app

    return build
