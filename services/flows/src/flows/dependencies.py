"""flows service DI — everything the routes need, read off `app.state` where the lifespan put it.

`Annotated[X, Depends(getter)]` aliases, so a route signature names the resource and nothing else. Reading `app.state`
in a route body instead would make the route untestable without a full app.
"""

from typing import Annotated

import httpx
from fastapi import Depends, Request

from flows.config import FlowsSettings
from flows.runner import RunRunner
from flows.store import FlowsStore
from service_kit.exceptions import ServiceUnavailableError


#: Why the run doors answer 503 on a process that names no run store.
NO_RUN_STORE = "this flows service names no run store (WORKBENCH_FLOWS_STATE_HOST), so it cannot start or read runs"


def get_flows_settings(request: Request) -> FlowsSettings:
    return request.app.state.flows_settings


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_store(request: Request) -> FlowsStore:
    """The run store, or a 503 naming why there is none."""
    store: FlowsStore | None = getattr(request.app.state, "run_store", None)
    if store is None:
        raise ServiceUnavailableError(NO_RUN_STORE)
    return store


def get_runner(request: Request) -> RunRunner:
    """This replica's run driver; present whenever the store is."""
    runner: RunRunner | None = getattr(request.app.state, "run_runner", None)
    if runner is None:
        raise ServiceUnavailableError(NO_RUN_STORE)
    return runner


FlowsSettingsDep = Annotated[FlowsSettings, Depends(get_flows_settings)]
HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
StoreDep = Annotated[FlowsStore, Depends(get_store)]
RunnerDep = Annotated[RunRunner, Depends(get_runner)]
