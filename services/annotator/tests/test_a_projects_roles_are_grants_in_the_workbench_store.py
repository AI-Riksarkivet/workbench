"""A project's roles are grants in the workbench's own OpenFGA store, and an organisation's standing is too.

The annotator's create and members doors over a store of the test's own on the pinned OpenFGA (root
`conftest.py::openfga_store`), the organisations declared as the model writer converges them. The project and tenant
actors are faked: the roles do not live in them. Every outbound HTTP call is refused by a strict respx router:
the annotator asks no lakehouse what a person may do in it.

acme: olga administers it, alice belongs to it; mallory belongs nowhere.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import respx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from annotator.annotations import save, wire
from annotator.api.security import authenticate, current_subject, get_authorizer
from annotator.api.v1.endpoints import me, members, projects
from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import ServiceUnavailableError, register_handlers
from service_kit.governed import fga
from service_kit.governed.identity import Caller
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from tests.media_catalog import write_table
from tests.openfga import OpenFgaStore


class _Actor:
    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        return doc

    async def register(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {}


class _Door:
    """The doors as one person after another calls them: `as_(person)` names who the next request is."""

    def __init__(self, client: TestClient, who: list[str]) -> None:
        self._client, self._who = client, who

    def as_(self, person: str) -> TestClient:
        self._who[0] = f"keycloak~{person}"
        return self._client


DOC = "a" * 16
DATASET = "acme$media"


@pytest.fixture
def door(openfga_store: OpenFgaStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Door]:
    write_table(
        tmp_path, ["acme", "media", "chunks"], pa.table({"doc_id": [DOC]}), descriptor={"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}}
    )
    openfga_store.write(("user:keycloak~olga", "admin", "org:acme"), ("user:keycloak~alice", "member", "org:acme"))
    monkeypatch.setattr(projects, "_create_actor", lambda _p: _Actor())
    monkeypatch.setattr(projects, "_tenant_actor", lambda _t: _Actor())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.resources = AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)}))
        app.state.authz = fga.Authorizer(
            fga.make_client(openfga_store.url, credential_file=None, store_id=openfga_store.store_id, model_id=openfga_store.model_id)
        )
        yield
        await app.state.authz.close()

    who = ["anon"]
    app = FastAPI(lifespan=lifespan)
    register_handlers(app)
    app.include_router(projects.router)
    app.include_router(members.router)
    app.include_router(save.router)
    app.include_router(wire.router)
    app.include_router(me.router)
    app.dependency_overrides[current_subject] = lambda: who[0]
    app.dependency_overrides[authenticate] = lambda: Caller(
        principal=Principal.parse(who[0]), claims=IDToken(iss="i", sub=who[0], aud="workbench", exp=1, iat=1), access_token=SecretStr("t")
    )
    app.dependency_overrides[get_authorizer] = _authz_of
    with respx.mock(assert_all_mocked=True, assert_all_called=False), TestClient(app) as client:
        yield _Door(client, who)


def _authz_of(request: Request) -> fga.Authorizer:
    return request.app.state.authz


def _create(door: _Door, person: str) -> Any:
    return door.as_(person).post("/projects", json={"tenant": "acme", "slug": "charters"})


def test_an_org_member_creates_a_project_and_owns_it_and_a_stranger_may_not(door: _Door, openfga_store: OpenFgaStore) -> None:
    created, refused = _create(door, "alice"), _create(door, "mallory")

    assert created.status_code == 201, created.text
    project = created.json()["project_id"]
    assert openfga_store.tuples(f"project:{project}") == {("org:acme", "org", f"project:{project}"), ("user:keycloak~alice", "owner", f"project:{project}")}
    assert refused.status_code == 403
    assert refused.json()["detail"] == "keycloak~mallory lacks can_create_project on org:acme"


def test_the_owner_alone_administers_the_owner_and_manager_roles_and_the_last_one_is_never_revoked(door: _Door) -> None:
    project = _create(door, "alice").json()["project_id"]
    members_of = f"/projects/{project}/members"

    assert door.as_("alice").put(members_of, json={"user": "keycloak~bob", "relation": "manager"}).status_code == 200
    assert door.as_("bob").put(members_of, json={"user": "keycloak~fay", "relation": "annotator"}).status_code == 200
    assert door.as_("bob").put(members_of, json={"user": "keycloak~bob", "relation": "owner"}).status_code == 403, "a manager made themselves owner"
    assert door.as_("bob").delete(members_of, params={"user": "keycloak~alice", "relation": "owner"}).status_code == 403, "a manager removed the owner"
    assert door.as_("fay").get(members_of).status_code == 403, "an annotator listed who holds the project"
    assert door.as_("olga").delete(members_of, params={"user": "keycloak~bob", "relation": "manager"}).status_code == 200
    refused = door.as_("olga").delete(members_of, params={"user": "keycloak~alice", "relation": "owner"})

    assert refused.status_code == 409, "the project's last owner or manager was revoked"
    listed = door.as_("alice").get(members_of).json()["members"]
    assert listed == [{"user": "keycloak~alice", "relation": "owner"}, {"user": "keycloak~fay", "relation": "annotator"}]


def test_a_revoke_that_cannot_read_the_project_leaves_its_owner(door: _Door, openfga_store: OpenFgaStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check comes before the delete: a store that answers the delete and not the read never loses the owner."""
    project = _create(door, "alice").json()["project_id"]

    async def unreadable(self: fga.Authorizer, obj: str) -> list[fga.Grant]:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga.Authorizer, "grants_on", unreadable)
    response = door.as_("alice").delete(f"/projects/{project}/members", params={"user": "keycloak~alice", "relation": "owner"})

    monkeypatch.undo()
    assert response.status_code == 503
    assert ("user:keycloak~alice", "owner", f"project:{project}") in openfga_store.tuples(f"project:{project}")


def _save(door: _Door, person: str, project: str, **body: Any) -> Any:
    unit = f"/annotations/{DOC}/0/19"
    return door.as_(person).post(unit, params={"project": project, "dataset": DATASET}, json={"base_version": 0, **body})


def _rows(door: _Door, project: str) -> list[str]:
    response = door.as_("fay").get(f"/annotations/{DOC}/0/19", params={"project": project, "dataset": DATASET})
    return sorted(pa.ipc.open_stream(response.content).read_all().column("id").to_pylist())


def test_a_project_member_cannot_write_or_delete_another_projects_annotations_of_a_shared_dataset(door: _Door, openfga_store: OpenFgaStore) -> None:
    """The confused deputy closed: a dataset two projects annotate holds no shared table the workbench writes for both.

    Fay annotates P1 and saves two rows on D. Mallory, an org member, makes P2 and binds D to it (as sending one item does),
    then asks the workbench to delete P1's rows: through P2 the ids are not P2's, through P1 she holds no role.
    """
    p1 = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{p1}"), ("user:keycloak~mallory", "member", "org:acme"))
    saved = _save(door, "fay", p1, inserts=[{"id": "p1-a", "shape_type": "bbox"}, {"id": "p1-b", "shape_type": "bbox"}])
    assert saved.status_code == 200, saved.text
    p2 = _create(door, "mallory").json()["project_id"]
    openfga_store.write((f"project:{p2}", "project", f"corpus:{DATASET}"))

    through_p2 = _save(door, "mallory", p2, deletes=["p1-a"])
    through_p1 = _save(door, "mallory", p1, deletes=["p1-a"])

    assert (through_p2.status_code, through_p1.status_code) == (404, 403), (through_p2.text, through_p1.text)
    assert _rows(door, p1) == ["p1-a", "p1-b"]


def test_me_is_the_persons_standing_in_each_organisation_the_store_holds(door: _Door) -> None:
    by_admin, by_member, by_stranger = (door.as_(person).get("/me").json() for person in ("olga", "alice", "mallory"))

    assert (by_admin["estate_admin"], by_admin["projects"]) == (True, [{"project": "acme", "role": "admin"}])
    assert (by_member["estate_admin"], by_member["projects"]) == (False, [{"project": "acme", "role": "member"}])
    assert (by_stranger["sub"], by_stranger["estate_admin"], by_stranger["projects"]) == ("keycloak~mallory", False, [])
