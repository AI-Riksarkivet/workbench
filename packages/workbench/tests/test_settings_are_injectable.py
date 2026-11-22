"""`make_service_app(settings=...)` builds the app from the settings it is given.

The object passed IS the app's settings: it reaches `app.state.settings`, the router prefix, the docs URLs, the
middleware and the service's lifespan, so a test, a script or a second app in the same process says what the settings
are instead of mutating the environment before an import. Omitted, the factory reads them from the environment
(`workbench.app.build_settings`).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from workbench.app import make_service_app
from workbench.config import Settings


def _router() -> APIRouter:
    router = APIRouter()

    @router.get("/where")
    async def where(request: Request) -> dict[str, str]:
        return {"prefix": request.app.state.settings.api_prefix}

    return router


def _injected() -> Settings:
    return Settings.model_validate({"WORKBENCH_API_PREFIX": "/api/injected", "WORKBENCH_DOCS": True, "WORKBENCH_MAX_BODY_BYTES": 4096})


def test_the_injected_settings_reach_the_router_prefix_and_the_docs_urls() -> None:
    settings = _injected()
    app = make_service_app(title="injected", routers=[_router()], settings=settings)
    with TestClient(app) as client:
        assert client.get("/api/injected/where").status_code == 200
        assert client.get("/api/injected/where").json() == {"prefix": "/api/injected"}
        assert client.get("/api/injected/openapi.json").status_code == 200


def test_the_injected_settings_reach_a_service_lifespan() -> None:
    seen: list[Settings] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        seen.append(app.state.settings)
        yield

    settings = _injected()
    with TestClient(make_service_app(title="injected", routers=[APIRouter()], lifespan=lifespan, settings=settings)):
        pass
    assert seen == [settings]
