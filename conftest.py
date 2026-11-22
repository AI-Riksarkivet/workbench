"""What every offline suite in the workspace shares.

At import, before collection builds any app, the media plane is pointed at an empty lance-namespace directory catalog
under the system temp dir, declared as the deployment's one source (``WORKBENCH_SOURCES``, with ``setdefault``): the
media settings refuse to load naming no source, and the offline suites run on the stock ``dir`` catalog the media code runs on unchanged. A test whose claim is
about the datasets builds them under its own ``tmp_path`` (``tests.media_catalog``) and names that root in the settings
it builds. Authentication, ON by default, is turned off for the offline suites the one way a deployment can
(``WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED``), so a test that signs a person in turns it on itself. A Ray a test
starts runs its workers on this interpreter (``RAY_ENABLE_UV_RUN_RUNTIME_ENV``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator

import pytest
import respx

from tests.identity import Provider
from tests.openfga import OpenFgaServer, OpenFgaStore, openfga_binary
from tests.postgres import PostgresServer, StateDatabase, postgres_tree


os.environ.setdefault(
    "WORKBENCH_SOURCES",
    json.dumps([{"kind": "rask", "name": "lake", "dir": tempfile.mkdtemp(prefix="workbench-media-catalog-"), "identity": "none"}]),
)
# Authentication is on by default; the offline suites build their apps unauthenticated unless a test turns it on, and say
# so the one way a deployment can.
os.environ.setdefault("WORKBENCH_OIDC_ENABLED", "false")
os.environ.setdefault("WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED", "true")
# A Ray a test starts runs its workers on this interpreter. Run under `uv run`, Ray would start each worker through
# `uv run` on an uploaded copy of the working directory instead, where none starts (measured on Ray 2.60.0). Ray reads
# this once, when it is first imported, so it is set here, before any test module imports it.
os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")


@pytest.fixture
def respx_mock() -> Iterator[respx.MockRouter]:
    """A respx router every route of which must be called and every unrouted call refused, for one test.

    Overrides respx's own `respx_mock` fixture, which without a `respx` marker hands out the module-level router built
    with `assert_all_called=False`, so a route registered for a call the code never makes would pass silently.
    """
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:  # noqa: TID251 - the one strict router
        yield router


@pytest.fixture(scope="session")
def openfga(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OpenFgaServer]:
    """One pinned OpenFGA per worker; every test takes its own store on it (`openfga_store`)."""
    server = OpenFgaServer(openfga_binary(), tmp_path_factory.mktemp("openfga"))
    yield server
    server.close()


@pytest.fixture
def openfga_store(openfga: OpenFgaServer) -> OpenFgaStore:
    """A store of this test's own holding the workbench model, so no test reads another's tuples."""
    return OpenFgaStore(openfga.url)


@pytest.fixture(scope="session")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    """One pinned PostgreSQL per worker; every test takes its own database on it (`state_database`)."""
    server = PostgresServer(postgres_tree(), tmp_path_factory.mktemp("postgres"))
    yield server
    server.close()


@pytest.fixture
def state_database(postgres: PostgresServer) -> StateDatabase:
    """A database of this test's own, so no test reads another's rows."""
    return StateDatabase(postgres)


@pytest.fixture
def oidc_provider() -> Iterator[Provider]:
    """An OIDC provider on a loopback port that issues DPoP-bound tokens and answers token exchanges."""
    running = Provider()
    yield running
    running.close()


@pytest.fixture(autouse=True)
def no_refresher_outlives_its_test() -> Iterator[None]:
    """Every OIDC verifier a test started is closed by its end (an app's shutdown does it, `OIDCVerifier.close`): a
    refresher left running would go on reading an issuer the next test does not know of."""
    yield
    left = [thread for thread in threading.enumerate() if thread.name == "oidc-refresher" and thread.is_alive()]
    assert left == [], f"{len(left)} OIDC refresher thread(s) left running"


@pytest.fixture(autouse=True)
def no_telemetry_outlives_its_test() -> Iterator[None]:
    """An app that exported telemetry took it down at its shutdown (`workbench.otel.Telemetry.close`): no OTel log
    handler is left on the root logger. The SDK's process-wide providers are set once per process, so they are put back
    unset here, as OpenTelemetry's own test utilities do: the next test's app installs its own."""
    from opentelemetry import trace
    from opentelemetry._logs import _internal as logs_internal
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    yield
    handlers = [handler for handler in logging.getLogger().handlers if isinstance(handler, LoggingHandler)]
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
    for module, provider, once in (
        (trace, "_TRACER_PROVIDER", "_TRACER_PROVIDER_SET_ONCE"),
        (metrics_internal, "_METER_PROVIDER", "_METER_PROVIDER_SET_ONCE"),
        (logs_internal, "_LOGGER_PROVIDER", "_LOGGER_PROVIDER_SET_ONCE"),
    ):
        setattr(module, provider, None)
        setattr(module, once, Once())
    assert handlers == [], f"{len(handlers)} OTel log handler(s) left on the root logger"
