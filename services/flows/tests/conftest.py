"""The flows service's offline tests: its own Postgres, its real lifespan, and a Serve stand-in.

`flows_state` names a database of the test's own (root `conftest.py::state_database`) in the flows settings, the way a
deployment's environment does, so the service's own lifespan (`flows.lifespan`) opens its run store on it and starts
its run driver. `client` is the real flows app (`make_service_app` with that lifespan) under a `TestClient`.

`serve` is a Serve stand-in on the loopback (`tests.flows_runs.ServeStandIn`), for a test whose claim crosses a process
or two drivers. A test whose claim stays in one process mocks Serve at the TRANSPORT (respx) instead.

What the autouse fixture pins, and why, is defence against the dev `.env` and the developer's shell:

* `WORKBENCH_FLOWS_SERVE_URL` — an address that is never dialled unless a test routes it: `.invalid` is the reserved TLD
  (RFC 2606), so an escaped call fails as a DNS error instead of reaching something.
* `WORKBENCH_FLOWS_SERVE_TOKEN_FILE` — a file holding a token, as the projected ServiceAccount token is mounted, so a
  Serve call carries a credential (`flows.serve_auth`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flows import executor, health, routes
from flows.lifespan import make_lifespan
from service_kit import make_service_app
from tests.flows_runs import ServeStandIn, state_env
from tests.postgres import StateDatabase


@pytest.fixture(autouse=True)
def _flows_env(tmp_path: Path) -> Iterator[None]:
    token = tmp_path / "serve-token"
    token.write_text("flows-sa-token")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WORKBENCH_FLOWS_SERVE_URL", "http://serve.invalid:8000")
        mp.setenv("WORKBENCH_FLOWS_SERVE_TOKEN_FILE", str(token))
        yield


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait a Serve retry takes, recorded instead of slept, so a retry costs a test no time and its delays can be
    asserted."""
    taken: list[float] = []

    async def wait(seconds: float) -> None:
        taken.append(seconds)

    monkeypatch.setattr(executor, "_sleep", wait)
    return taken


@pytest.fixture
def flows_state(state_database: StateDatabase, monkeypatch: pytest.MonkeyPatch) -> StateDatabase:
    """This test's database, named in the flows settings as a deployment's environment names it."""
    for name, value in state_env(state_database).items():
        monkeypatch.setenv(name, value)
    return state_database


@pytest.fixture
def client(flows_state: StateDatabase, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real flows app on its own Postgres, its routers mounted under `/api` as the chart and the local stack mount
    them."""
    monkeypatch.setenv("WORKBENCH_API_PREFIX", "/api")
    app = make_service_app(title="flows", routers=[health.router, routes.router], lifespan=make_lifespan)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServeStandIn]:
    """A Serve stand-in on the loopback, named as the flows service's Serve origin."""
    stand_in = ServeStandIn()
    monkeypatch.setenv("WORKBENCH_FLOWS_SERVE_URL", stand_in.url)
    yield stand_in
    stand_in.close()
