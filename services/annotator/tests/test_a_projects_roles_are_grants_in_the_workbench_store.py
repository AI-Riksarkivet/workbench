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

from annotator.annotations import save, tags, wire
from annotator.api.security import authenticate, current_subject, get_authorizer
from annotator.api.v1.endpoints import me, members, project_events, projects
from annotator.api.v1.endpoints import tasks as tasks_ep
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
    """The project and tenant actors, holding the one project a test creates."""

    def __init__(self) -> None:
        self.doc: dict[str, Any] | None = None

    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.doc = doc
        return doc

    async def get(self) -> dict[str, Any] | None:
        return self.doc

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.registered = [*getattr(self, "registered", []), str(payload["project_id"])]
        return {}

    async def list_projects(self) -> dict[str, Any]:
        ids = getattr(self, "registered", [])
        return {"project_ids": ids, "total": len(ids)}


class _Task:
    """One task's actor: the document a test seeds into the door's `tasks`."""

    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc

    async def get(self) -> dict[str, Any] | None:
        return self.doc


class _Door:
    """The doors as one person after another calls them: `as_(person)` names who the next request is."""

    def __init__(self, client: TestClient, who: list[str], tasks: dict[str, dict[str, Any]]) -> None:
        self._client, self._who, self.tasks = client, who, tasks

    def as_(self, person: str) -> TestClient:
        self._who[0] = f"keycloak~{person}"
        return self._client


DOC = "a" * 16
DATASET = "acme$media"
#: The unit a task of the test's project names, and one it does not.
UNIT = f"{DOC}/0/19"
OTHER_UNIT = f"{DOC}/0/20"


@pytest.fixture
def door(openfga_store: OpenFgaStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Door]:
    chunks = pa.table({"doc_id": [DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())})
    write_table(tmp_path, ["acme", "media", "chunks"], chunks, descriptor={"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}})
    openfga_store.write(("user:keycloak~olga", "admin", "org:acme"), ("user:keycloak~alice", "member", "org:acme"))
    actor = _Actor()
    monkeypatch.setattr(projects, "_create_actor", lambda _p: actor)
    monkeypatch.setattr(projects, "_tenant_actor", lambda _t: actor)
    monkeypatch.setattr(project_events, "_project_proxy", lambda _p: actor)
    tasks: dict[str, dict[str, Any]] = {}
    monkeypatch.setattr(tasks_ep, "_proxy", lambda task_id: _Task(tasks.get(task_id)))

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
    app.include_router(tags.router)
    app.include_router(save.router)
    app.include_router(wire.router)
    app.include_router(me.router)
    app.dependency_overrides[current_subject] = lambda: who[0]
    app.dependency_overrides[authenticate] = lambda: Caller(
        principal=Principal.parse(who[0]), claims=IDToken(iss="i", sub=who[0], aud="workbench", exp=1, iat=1), access_token=SecretStr("t")
    )
    app.dependency_overrides[get_authorizer] = _authz_of
    with respx.mock(assert_all_mocked=True, assert_all_called=False), TestClient(app) as client:
        yield _Door(client, who, tasks)


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


def _task(door: _Door, project: str, task_id: str = "t1", **over: Any) -> None:
    """Seed a task of `project` whose item is `UNIT`: claimed by fay unless `over` says otherwise."""
    door.tasks[task_id] = {
        "task_id": task_id,
        "project_id": project,
        "state": "claimed",
        "assignee": "keycloak~fay",
        "submitted_by": None,
        "source": {"kind": "chunks", "keys": [UNIT], "where": DATASET},
        "media": {"kind": "image"},
        **over,
    }


def _save(door: _Door, person: str, project: str, *, task: str = "t1", unit: str = UNIT, **body: Any) -> Any:
    params = {"project": project, "task": task, "dataset": DATASET}
    return door.as_(person).post(f"/annotations/{unit}", params=params, json={"base_version": 0, **body})


def _read(door: _Door, person: str, project: str, *, unit: str = UNIT) -> Any:
    return door.as_(person).get(f"/annotations/{unit}", params={"project": project, "task": "t1", "dataset": DATASET})


def _rows(door: _Door, project: str) -> list[str]:
    return sorted(pa.ipc.open_stream(_read(door, "fay", project).content).read_all().column("id").to_pylist())


def test_a_project_member_cannot_write_or_delete_another_projects_annotations_of_a_shared_dataset(door: _Door, openfga_store: OpenFgaStore) -> None:
    """The confused deputy closed: a dataset two projects annotate holds no shared table the workbench writes for both.

    Fay holds P1's task t1 and saves two rows of its unit. Mallory, an org member, makes P2 and binds the dataset to it,
    then asks the workbench to delete P1's rows: through P2 the task is not P2's, and on P1 she holds no role.
    """
    p1 = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{p1}"), ("user:keycloak~mallory", "member", "org:acme"))
    _task(door, p1)
    saved = _save(door, "fay", p1, inserts=[{"id": "p1-a", "shape_type": "bbox"}, {"id": "p1-b", "shape_type": "bbox"}])
    assert saved.status_code == 200, saved.text
    p2 = _create(door, "mallory").json()["project_id"]
    openfga_store.write((f"project:{p2}", "project", f"corpus:{DATASET}"))

    through_p2 = _save(door, "mallory", p2, deletes=["p1-a"])
    through_p1 = _save(door, "mallory", p1, deletes=["p1-a"])

    assert (through_p2.status_code, through_p2.json()["detail"]) == (404, "No task t1 that you can see")
    # An org member sees the org's projects and their tasks, and annotates none she holds no role on.
    assert (through_p1.status_code, through_p1.json()["detail"]) == (403, "keycloak~mallory lacks can_annotate on task:t1")
    assert _rows(door, p1) == ["p1-a", "p1-b"]


@pytest.mark.parametrize(
    ("task", "person", "unit", "status", "detail"),
    [
        pytest.param({}, "fay", OTHER_UNIT, 404, f"unit {OTHER_UNIT} of dataset {DATASET} is not task t1's", id="a-unit-the-task-does-not-hold"),
        pytest.param(
            {"source": {"kind": "chunks", "keys": [f"{DOC}/0"], "where": DATASET}},
            "fay",
            f"{DOC}/0",
            422,
            f"unit key '{DOC}/0' has 2 part(s), and this dataset names a unit by 3: doc_id/speech_id/chunk_id",
            id="a-key-the-dataset-cannot-name",
        ),
        pytest.param(
            {"state": "accepted"}, "fay", UNIT, 409, "task t1 is accepted — its shapes take edits only while it is claimed or in review", id="a-finished-task"
        ),
        pytest.param({"assignee": "keycloak~rita"}, "fay", UNIT, 403, "the task is held by keycloak~rita", id="another-annotators-claim"),
        pytest.param({"assignee": "keycloak~vic"}, "vic", UNIT, 403, "keycloak~vic lacks can_annotate on task:t1", id="a-viewer-holding-a-claim"),
        pytest.param(
            {"state": "in_review", "submitted_by": "keycloak~rita"},
            "rita",
            UNIT,
            403,
            "a reviewer may not edit their own submission",
            id="a-reviewer-on-their-own-work",
        ),
        pytest.param({"state": "in_review", "submitted_by": "keycloak~fay"}, "rita", UNIT, 200, None, id="a-reviewer-fixing-work-in-review"),
    ],
)
def test_a_save_is_an_edit_the_named_task_takes_from_its_caller_now(
    door: _Door, openfga_store: OpenFgaStore, task: dict[str, Any], person: str, unit: str, status: int, detail: str | None
) -> None:
    """A save names its project, task, dataset and unit, and each is read against the task's own record: the unit is the
    task's, the key one the dataset can name, the task's state takes edits, and the caller holds the relation the state
    asks and is who may edit it now. The holder of a claim writes, and so does a reviewer fixing work in review."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(
        ("user:keycloak~fay", "annotator", f"project:{project}"),
        ("user:keycloak~rita", "reviewer", f"project:{project}"),
        ("user:keycloak~vic", "viewer", f"project:{project}"),
    )
    _task(door, project, **task)

    response = _save(door, person, project, unit=unit, inserts=[{"id": "n1", "shape_type": "bbox"}])

    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json()["detail"] == detail
    if status != 422:  # that task's own unit is the key no read can name either
        assert _rows(door, project) == (["n1"] if status == 200 else [])


def test_a_tag_batch_tags_only_the_units_of_the_task_it_names(door: _Door, openfga_store: OpenFgaStore) -> None:
    """The tag door writes across units in one version, so each unit it tags is checked against the task: a batch
    naming a unit the task does not hold writes nothing."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{project}"))
    _task(door, project)

    def tag(*keys: int) -> Any:
        body = {"adds": [{"doc_id": DOC, "keys": list(keys), "labels": ["letter"]}], "removes": [], "base_version": 0}
        return door.as_("fay").post("/annotations/tags", params={"project": project, "task": "t1", "dataset": DATASET}, json=body)

    outside, inside = tag(0, 20), tag(0, 19)

    assert (outside.status_code, outside.json()["detail"]) == (404, f"unit {OTHER_UNIT} of dataset {DATASET} is not task t1's")
    assert inside.status_code == 200, inside.text
    assert _rows(door, project) == [f"tag:{DOC}:0:19:letter"]


def test_a_viewer_reads_the_unit_of_a_task_and_no_other(door: _Door, openfga_store: OpenFgaStore) -> None:
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~vic", "viewer", f"project:{project}"))
    _task(door, project)

    own, other = _read(door, "vic", project), _read(door, "vic", project, unit=OTHER_UNIT)

    assert own.status_code == 200, own.text
    assert (other.status_code, other.json()["detail"]) == (404, f"unit {OTHER_UNIT} of dataset {DATASET} is not task t1's")


def test_me_is_the_persons_standing_in_each_organisation_the_store_holds(door: _Door) -> None:
    by_admin, by_member, by_stranger = (door.as_(person).get("/me").json() for person in ("olga", "alice", "mallory"))

    assert (by_admin["estate_admin"], by_admin["orgs"]) == (True, [{"org": "acme", "role": "admin"}])
    assert (by_member["estate_admin"], by_member["orgs"]) == (False, [{"org": "acme", "role": "member"}])
    assert (by_stranger["sub"], by_stranger["estate_admin"], by_stranger["orgs"]) == ("keycloak~mallory", False, [])


def test_a_projects_page_names_what_its_reader_may_do_there(door: _Door, openfga_store: OpenFgaStore) -> None:
    """The project's read answers the caller's own permissions on it, from the store, so a page offers an action only to
    someone the door that runs it would let through; the door still checks on its own."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{project}"), ("user:keycloak~rita", "reviewer", f"project:{project}"))

    held = {person: door.as_(person).get(f"/projects/{project}") for person in ("alice", "olga", "rita", "fay", "mallory")}

    owner = ["can_administer", "can_annotate", "can_claim", "can_manage", "can_publish", "can_review", "can_send_items"]
    assert {person: r.json()["permissions"] for person, r in held.items() if r.status_code == 200} == {
        "alice": owner,
        "olga": owner,
        "rita": ["can_annotate", "can_claim", "can_review", "can_send_items"],
        "fay": ["can_annotate", "can_claim", "can_send_items"],
    }
    assert (held["mallory"].status_code, held["mallory"].json()["detail"]) == (404, f"No project {project} that you can see")


def test_a_listing_narrowed_to_sending_names_only_the_projects_the_caller_may_send_to(door: _Door, openfga_store: OpenFgaStore) -> None:
    """An org member views every project of the org and may send to none they hold no role on: `?can=can_send_items`
    lists only those the store says they may send items to."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(
        ("user:keycloak~fay", "annotator", f"project:{project}"), ("user:keycloak~fay", "member", "org:acme"), ("user:keycloak~vic", "member", "org:acme")
    )

    def listed(person: str, **query: str) -> list[str]:
        r = door.as_(person).get("/projects", params={"tenant": "acme", **query})
        return [p["project_id"] for p in r.json()["projects"]]

    assert (listed("vic"), listed("vic", can="can_send_items"), listed("fay", can="can_send_items")) == ([project], [], [project])
