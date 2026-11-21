"""`POST /projects` writes the project's grants first, then its row: a failed row write takes the grants back.

The grants lead so a project never exists that nobody may manage. A grant on a project with no row answers nothing but
a 404, and `project_id` is minted per attempt, so a retry makes a second project: the first attempt's grants would
name an id nothing will ever hold. So a create whose row write fails removes what it granted, and then fails.

The grants go to a real OpenFGA store of the test's own and the row to a real PostgreSQL database; the row write is
made to fail by closing the store's pool under the running app.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.core.config import AnnotatorSettings
from annotator.state.store import AnnotatorStore
from tests.openfga import OpenFgaStore
from workbench.exceptions import register_handlers
from workbench.governed import fga


CREATOR = "keycloak~gina"


@pytest.fixture
def client(openfga_store: OpenFgaStore, state_settings: AnnotatorSettings) -> Iterator[TestClient]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.authz = fga.Authorizer(
            fga.make_client(openfga_store.url, credential_file=None, store_id=openfga_store.store_id, model_id=openfga_store.model_id)
        )
        app.state.store = await AnnotatorStore.open(state_settings)
        yield
        await app.state.store.close()
        await app.state.authz.close()

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        return True

    def authz_of(request: Request) -> fga.Authorizer:
        return request.app.state.authz

    app = FastAPI(lifespan=lifespan)
    app.include_router(projects_ep.router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = authz_of
    app.dependency_overrides[current_subject] = lambda: CREATOR
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _create(client: TestClient) -> Any:
    return client.post("/projects", json={"tenant": "acme", "slug": "charters"})


def _owned(openfga_store: OpenFgaStore) -> set[str]:
    """The projects the creator owns, as the store answers it."""

    async def _list() -> set[str]:
        authz = fga.Authorizer(fga.make_client(openfga_store.url, credential_file=None, store_id=openfga_store.store_id, model_id=openfga_store.model_id))
        try:
            return set(await authz.list_objects(user=CREATOR, relation="owner", object_type="project"))
        finally:
            await authz.close()

    return asyncio.run(_list())


def test_a_failed_row_write_takes_the_grants_back(client: TestClient, openfga_store: OpenFgaStore) -> None:
    app: Any = client.app
    portal = client.portal
    assert portal is not None, "the client is not running the app"
    portal.call(app.state.store.close)

    response = _create(client)

    assert response.status_code == 500
    assert _owned(openfga_store) == set(), "a grant survived a create whose row was never written"


def test_a_create_that_succeeds_keeps_its_grants(client: TestClient, openfga_store: OpenFgaStore) -> None:
    """The failure mode that would hide the fix: removing the grants unconditionally also passes above."""
    response = _create(client)

    assert response.status_code == 201, response.text
    project = f"project:{response.json()['project_id']}"
    assert openfga_store.tuples(project) == {("org:acme", "org", project), (f"user:{CREATOR}", "owner", project)}
    assert _owned(openfga_store) == {project}
