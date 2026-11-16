"""What every offline suite in the workspace shares.

At import, before collection builds any app, the media plane is pointed at an empty lance-namespace directory catalog
under the system temp dir, declared as the deployment's one source (``WORKBENCH_SOURCES``, with ``setdefault``): the
media settings refuse to load naming no source, and the offline suites run on the stock ``dir`` catalog the media code runs on unchanged. A test whose claim is
about the datasets builds them under its own ``tmp_path`` (``tests.media_catalog``) and names that root in the settings
it builds. Authentication, ON by default, is turned off for the offline suites the one way a deployment can
(``WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED``), so a test that signs a person in turns it on itself.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator

import pytest

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
