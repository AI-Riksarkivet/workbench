"""A project's roles are grants in the workbench's own OpenFGA store, and an organisation's standing is too.

The annotator's create and members doors over a store of the test's own on the pinned OpenFGA (root
`conftest.py::openfga_store`), the organisations declared as the model writer converges them. The projects and tasks
live in a PostgreSQL database of the test's own (`state_settings`); the roles do not. Every outbound HTTP call is refused by a strict respx router:
the annotator asks no lakehouse what a person may do in it.

acme: olga administers it, alice belongs to it; mallory belongs nowhere.
"""

from __future__ import annotations

import json
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

from annotator.annotations import save, tags, versions, wire
from annotator.api.security import authenticate, current_subject, get_authorizer
from annotator.api.v1.endpoints import me, members, projects
from annotator.api.v1.endpoints import tasks as task_doors
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import Task
from annotator.state.store import AnnotatorStore
from tests.media_catalog import lakehouse_dir, write_table
from tests.openfga import OpenFgaStore
from workbench.exceptions import ServiceUnavailableError, register_handlers
from workbench.governed import fga
from workbench.governed.identity import Caller
from workbench.governed.oidc import IDToken
from workbench.governed.principal import Principal
from workbench.media.config import MediaSettings
from workbench.media.state import AppState


class _Door:
    """The doors as one person after another calls them: `as_(person)` names who the next request is."""

    def __init__(self, client: TestClient, app: FastAPI, who: list[str]) -> None:
        self._client, self._app, self._who = client, app, who

    def seed(self, task: Task) -> None:
        """Write a task into the store as a send does, on the app's own loop."""
        portal = self._client.portal
        assert portal is not None, "the client is not running the app"
        portal.call(self._app.state.store.send_tasks, task.project_id, [task])

    def as_(self, person: str) -> TestClient:
        self._who[0] = f"keycloak~{person}"
        return self._client


DOC = "a" * 16
#: The name claims each person's token carries beyond the subject: alice's name with a control character and runs of
#: spaces in it, bob a username and an address, rex only an address, dave a name.
CLAIMS: dict[str, dict[str, str]] = {
    "keycloak~alice": {"name": " Alice\u0007  Ek ", "email": "alice@acme.example"},
    "keycloak~bob": {"preferred_username": "bob", "email": "bob@acme.example"},
    "keycloak~rex": {"email": "rex@acme.example"},
    "keycloak~dave": {"name": "Dave Ström"},
}
DATASET = "lake~acme$media"
#: The unit a task of the test's project names, and one it does not.
UNIT = f"{DOC}/0/19"
OTHER_UNIT = f"{DOC}/0/20"


@pytest.fixture
def door(openfga_store: OpenFgaStore, state_settings: AnnotatorSettings, tmp_path: Path) -> Iterator[_Door]:
    chunks = pa.table({"doc_id": [DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())})
    write_table(tmp_path, ["acme", "media", "chunks"], chunks, descriptor={"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}})
    openfga_store.write(("user:keycloak~olga", "admin", "org:acme"), ("user:keycloak~alice", "member", "org:acme"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.resources = AppState(settings=MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}))
        app.state.authz = fga.Authorizer(
            fga.make_client(openfga_store.url, credential_file=None, store_id=openfga_store.store_id, model_id=openfga_store.model_id)
        )
        app.state.store = await AnnotatorStore.open(state_settings)
        yield
        await app.state.store.close()
        await app.state.authz.close()

    who = ["anon"]
    app = FastAPI(lifespan=lifespan)
    register_handlers(app)
    app.include_router(projects.router)
    app.include_router(members.router)
    app.include_router(tags.router)
    app.include_router(save.router)
    app.include_router(versions.router)
    app.include_router(wire.router)
    app.include_router(me.router)
    app.include_router(task_doors.router)
    app.dependency_overrides[current_subject] = lambda: who[0]
    app.dependency_overrides[authenticate] = lambda: Caller(
        principal=Principal.parse(who[0]),
        claims=IDToken.model_validate({"iss": "i", "sub": who[0], "aud": "workbench", "exp": 1, "iat": 1, **CLAIMS.get(who[0], {})}),
        access_token=SecretStr("t"),
    )
    app.dependency_overrides[get_authorizer] = _authz_of
    with respx.mock(assert_all_mocked=True, assert_all_called=False), TestClient(app) as client:
        yield _Door(client, app, who)


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
    # The words a person reads name no principal; who, what and where are the problem's machine fields.
    assert {k: refused.json()[k] for k in ("detail", "principal", "relation", "object")} == {
        "detail": "you lack can_create_project on this organisation",
        "principal": "keycloak~mallory",
        "relation": "can_create_project",
        "object": "org:acme",
    }


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
    document: dict[str, Any] = {
        "task_id": task_id,
        "project_id": project,
        "state": "claimed",
        "assignee": "keycloak~fay",
        "submitted_by": None,
        "source": {"kind": "chunks", "keys": [UNIT], "where": DATASET},
        "media": {"kind": "image"},
        **over,
    }
    door.seed(Task.model_validate(document))


def _save(door: _Door, person: str, project: str, *, task: str = "t1", unit: str = UNIT, **body: Any) -> Any:
    params = {"project": project, "task": task, "dataset": DATASET}
    return door.as_(person).post(f"/annotations/{unit}", params=params, json={"base_version": 0, **body})


def _read(door: _Door, person: str, project: str, *, task: str = "t1", unit: str = UNIT) -> Any:
    return door.as_(person).get(f"/annotations/{unit}", params={"project": project, "task": task, "dataset": DATASET})


def _rows(door: _Door, project: str, *, task: str = "t1", person: str = "fay") -> list[str]:
    return sorted(pa.ipc.open_stream(_read(door, person, project, task=task).content).read_all().column("id").to_pylist())


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
    assert (through_p1.status_code, through_p1.json()["detail"]) == (403, "you lack can_annotate on this item")
    assert _rows(door, p1) == ["p1-a", "p1-b"]


@pytest.mark.parametrize(
    ("task", "person", "unit", "status", "detail"),
    [
        pytest.param({}, "fay", OTHER_UNIT, 404, f"unit {OTHER_UNIT} of dataset {DATASET} is not this item's", id="a-unit-the-task-does-not-hold"),
        pytest.param(
            {"source": {"kind": "chunks", "keys": [f"{DOC}/0"], "where": DATASET}},
            "fay",
            f"{DOC}/0",
            422,
            f"unit key '{DOC}/0' has 2 part(s), and this dataset names a unit by 3: doc_id/speech_id/chunk_id",
            id="a-key-the-dataset-cannot-name",
        ),
        pytest.param(
            {"state": "accepted"}, "fay", UNIT, 409, "this item is accepted — its shapes take edits only while it is claimed or in review", id="a-finished-task"
        ),
        pytest.param({"assignee": "keycloak~rita"}, "fay", UNIT, 403, "another person holds this item", id="another-annotators-claim"),
        pytest.param({"assignee": "keycloak~vic"}, "vic", UNIT, 403, "you lack can_annotate on this item", id="a-viewer-holding-a-claim"),
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


_LONG_GROUP = "g" * 129
_MANY = ",".join(f'{{"id":"d{i}","t_start":0,"t_end":1}}' for i in range(5001))


@pytest.mark.parametrize(
    ("body", "status", "own"),
    [
        pytest.param('{"temporal":[{"id":"held","t_start":NaN,"t_end":1}]}', 422, False, id="a-time-that-is-not-a-number"),
        pytest.param('{"inserts":[{"id":"n","shape_type":"segment","t_start":0,"t_end":Infinity}]}', 422, False, id="an-infinite-end"),
        pytest.param('{"inserts":[{"id":"n","shape_type":"segment","t_start":9,"t_end":3}]}', 422, False, id="an-end-before-its-start"),
        pytest.param('{"temporal":[{"id":"held","t_start":-1,"t_end":1}]}', 422, False, id="a-moment-before-the-media"),
        pytest.param('{"inserts":[{"id":"n","shape_type":"bbox","x":NaN}]}', 422, False, id="a-box-at-no-place"),
        pytest.param(f'{{"inserts":[{{"id":"n","shape_type":"bbox","group":"{_LONG_GROUP}"}}]}}', 422, False, id="a-group-past-its-bound"),
        pytest.param(f'{{"temporal":[{_MANY}]}}', 422, False, id="more-rows-than-one-save-names"),
        pytest.param('{"spans":[{"id":"held","char_start":5,"char_end":2}]}', 422, False, id="a-span-that-ends-before-it-starts"),
        pytest.param('{"spans":[{"id":"held","char_start":-3,"char_end":2}]}', 422, False, id="a-span-before-its-text"),
        pytest.param(
            '{"inserts":[{"id":"n","shape_type":"text","parent_id":"held","char_start":0,"char_end":2147483648}]}', 422, False, id="a-span-past-int32"
        ),
        pytest.param('{"inserts":[{"id":"n","shape_type":"text","parent_id":"held","char_start":-1,"char_end":4}]}', 422, False, id="half-a-span"),
        pytest.param('{"inserts":[{"id":"n","shape_type":"rectangle"}]}', 422, False, id="a-shape-type-the-work-does-not-speak"),
        # A delete or an edit is held to the named task's own work: another task's row is not this save's to touch.
        pytest.param('{"deletes":["held"]}', 404, False, id="a-delete-of-another-tasks-row"),
        pytest.param('{"edits":[{"id":"held","label":"taken"}]}', 404, False, id="an-edit-of-another-tasks-row"),
        # Through the task that holds it: a new shape takes an id of its own.
        pytest.param('{"inserts":[{"id":"held","shape_type":"bbox","label":"taken"}]}', 409, True, id="a-new-shape-with-an-id-the-work-holds"),
        # The revision the save was built on is the task's: an older one, or none, is refused (two tabs, a reviewer).
        pytest.param('{"base_version":0,"edits":[{"id":"held","label":"taken"}]}', 409, True, id="a-save-built-on-an-older-revision"),
        pytest.param('{"base_version":null,"edits":[{"id":"held","label":"taken"}]}', 409, True, id="a-save-that-names-no-revision"),
    ],
)
def test_a_save_of_impossible_times_or_another_tasks_row_is_refused_and_writes_nothing(
    door: _Door, openfga_store: OpenFgaStore, body: str, status: int, own: bool
) -> None:
    """Fay saves a segment on task t1's unit, then a save names something its task's work may not hold, through task t2
    of the same project (whose work is empty) or through t1 itself: the save is refused, and t1's row is as it was."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{project}"))
    _task(door, project)
    _task(door, project, "t2", source={"kind": "chunks", "keys": [OTHER_UNIT], "where": DATASET})
    first = _save(door, "fay", project, inserts=[{"id": "held", "shape_type": "segment", "t_start": 1.0, "t_end": 2.0, "label": "kept"}])
    assert first.status_code == 200, first.text
    task, unit = ("t1", UNIT) if own else ("t2", OTHER_UNIT)
    payload = {"base_version": first.json()["version"] if own else 0, **json.loads(body.replace("NaN", '"nan"').replace("Infinity", '"inf"'))}
    raw = json.dumps(payload).replace('"nan"', "NaN").replace('"inf"', "Infinity")

    refused = door.as_("fay").post(
        f"/annotations/{unit}",
        params={"project": project, "task": task, "dataset": DATASET},
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == status, refused.text
    rows = pa.ipc.open_stream(_read(door, "fay", project).content).read_all().select(["id", "label", "t_start", "t_end"]).to_pylist()
    assert rows == [{"id": "held", "label": "kept", "t_start": 1.0, "t_end": 2.0}]


def test_a_tag_batch_tags_only_the_units_of_the_task_it_names(door: _Door, openfga_store: OpenFgaStore) -> None:
    """The tag door writes into the named task's draft, so each unit it tags is checked against the task: a batch
    naming a unit the task does not hold writes nothing. A re-tag is idempotent and a removal takes the row."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~fay", "annotator", f"project:{project}"))
    _task(door, project)

    def tag(*keys: int) -> Any:
        body = {"adds": [{"doc_id": DOC, "keys": list(keys), "labels": ["letter"]}], "removes": []}
        return door.as_("fay").post("/annotations/tags", params={"project": project, "task": "t1", "dataset": DATASET}, json=body)

    outside, inside, again = tag(0, 20), tag(0, 19), tag(0, 19)

    assert (outside.status_code, outside.json()["detail"]) == (404, f"unit {OTHER_UNIT} of dataset {DATASET} is not this item's")
    assert (inside.status_code, again.status_code) == (200, 200), inside.text
    # A re-tag changes nothing, so it writes nothing: the work stays at the revision the first tag made.
    assert again.json()["version"] == inside.json()["version"]
    assert _rows(door, project) == [f"tag:t1:{DOC}:0:19:letter"]
    removed = door.as_("fay").post(
        "/annotations/tags",
        params={"project": project, "task": "t1", "dataset": DATASET},
        json={"adds": [], "removes": [{"doc_id": DOC, "keys": [0, 19], "labels": ["letter"]}]},
    )
    assert removed.status_code == 200, removed.text
    assert _rows(door, project) == []


def test_two_replicas_of_one_item_each_keep_their_own_work_and_only_a_reviewer_reads_both(door: _Door, openfga_store: OpenFgaStore) -> None:
    """A consensus item is two tasks over one unit, each an annotator's own: fay's and rita's saves land in their own
    task's draft, and each task's read, save and delete reach only its own rows. Fay may view rita's task, as everyone on
    the project may, and is still refused rita's rows and draft; rex, a reviewer, reads both versions apart."""
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(
        ("user:keycloak~fay", "annotator", f"project:{project}"),
        ("user:keycloak~rita", "annotator", f"project:{project}"),
        ("user:keycloak~rex", "reviewer", f"project:{project}"),
    )
    _task(door, project, "g1-r1", replica_of="g1")
    _task(door, project, "g1-r2", replica_of="g1", assignee="keycloak~rita")
    first = _save(door, "fay", project, task="g1-r1", inserts=[{"id": "fay-box", "shape_type": "bbox", "label": "a"}])
    # Each replica's work has its own revisions: fay's save moved hers, not rita's.
    second = _save(door, "rita", project, task="g1-r2", inserts=[{"id": "rita-box", "shape_type": "bbox", "label": "b"}], base_version=0)
    assert (first.status_code, second.status_code) == (200, 200), second.text

    across = _save(door, "rita", project, task="g1-r2", deletes=["fay-box"], base_version=second.json()["version"])

    peeked_rows = _read(door, "fay", project, task="g1-r2")
    peeked_draft = door.as_("fay").get("/tasks/g1-r2/draft")

    assert _rows(door, project, task="g1-r1", person="rex") == ["fay-box"]
    assert _rows(door, project, task="g1-r2", person="rex") == ["rita-box"]
    assert [s["shape_id"] for s in door.as_("rex").get("/tasks/g1-r2/draft").json()["shapes"]] == ["rita-box"]
    refusal = "you may not read this replica's work: only its annotator, or someone who may review it"
    assert (peeked_rows.status_code, peeked_rows.json()["detail"]) == (403, refusal)
    assert (peeked_draft.status_code, peeked_draft.json()["detail"]) == (403, refusal)
    assert across.status_code == 404, across.text
    assert across.json()["detail"] == "annotation(s) ['fay-box'] are not this task's rows of its unit"


def test_a_viewer_reads_the_unit_of_a_task_and_no_other(door: _Door, openfga_store: OpenFgaStore) -> None:
    project = _create(door, "alice").json()["project_id"]
    openfga_store.write(("user:keycloak~vic", "viewer", f"project:{project}"))
    _task(door, project)

    own, other = _read(door, "vic", project), _read(door, "vic", project, unit=OTHER_UNIT)

    assert own.status_code == 200, own.text
    assert (other.status_code, other.json()["detail"]) == (404, f"unit {OTHER_UNIT} of dataset {DATASET} is not this item's")


def test_me_is_the_persons_standing_in_each_organisation_the_store_holds(door: _Door) -> None:
    by_admin, by_member, by_stranger = (door.as_(person).get("/me").json() for person in ("olga", "alice", "mallory"))

    assert (by_admin["estate_admin"], by_admin["orgs"], by_admin["name"]) == (True, [{"org": "acme", "role": "admin"}], None)
    assert (by_member["estate_admin"], by_member["orgs"], by_member["name"]) == (False, [{"org": "acme", "role": "member"}], "Alice Ek")
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


def test_a_granter_is_offered_by_name_the_people_of_the_projects_they_manage_in_its_organisation(door: _Door, openfga_store: OpenFgaStore) -> None:
    """alice manages two projects of acme and not olga's third. She is offered who holds a role on her two and is known
    here by name (herself, and bob once he signed in): never dave, named but only on the project she may not manage, nor
    rex, who holds a role but whose token names nobody. bob, who manages nothing, is refused the list."""
    first, second = (_create(door, "alice").json()["project_id"] for _ in range(2))
    third = _create(door, "olga").json()["project_id"]
    door.as_("alice").put(f"/projects/{second}/members", json={"user": "keycloak~bob", "relation": "annotator"})
    door.as_("alice").put(f"/projects/{first}/members", json={"user": "keycloak~rex", "relation": "annotator"})
    door.as_("olga").put(f"/projects/{third}/members", json={"user": "keycloak~dave", "relation": "annotator"})
    for person in ("bob", "rex", "dave"):
        door.as_(person).get("/me")

    offered = door.as_("alice").get(f"/projects/{first}/members/candidates")
    refused = door.as_("bob").get(f"/projects/{second}/members/candidates")

    assert offered.json() == {
        "people": [{"principal": "keycloak~alice", "name": "Alice Ek"}, {"principal": "keycloak~bob", "name": "bob"}],
        "truncated": False,
    }
    assert (refused.status_code, refused.json()["detail"]) == (403, "you lack can_manage on this project")


def test_a_manager_of_more_projects_than_are_read_is_offered_the_people_of_the_one_granted_on(door: _Door, openfga_store: OpenFgaStore) -> None:
    """alice manages 101 projects, one more than a candidates read gathers. Granting on the newest, she is offered its
    own people (bob, who signed in) and told the set may be incomplete."""
    projects = [_create(door, "alice").json()["project_id"] for _ in range(101)]
    door.as_("alice").put(f"/projects/{projects[-1]}/members", json={"user": "keycloak~bob", "relation": "annotator"})
    door.as_("bob").get("/me")

    offered = door.as_("alice").get(f"/projects/{projects[-1]}/members/candidates").json()

    assert ([p["principal"] for p in offered["people"]], offered["truncated"]) == (["keycloak~alice", "keycloak~bob"], True)


def test_a_person_is_named_by_what_their_own_token_carried_when_they_last_signed_in_or_wrote(door: _Door, openfga_store: OpenFgaStore) -> None:
    """alice's create keeps her token's name (cleaned); bob, granted a role, is shown by his principal until he signs in
    (the app's one `/me` read registers him; reading a task keeps nothing) with the username his token carries; rex's
    token names only an address, which is never shown in place of his principal, whether he signs in or writes. The names
    ride beside the principals of the reads that show them: the members list, the task and its history of saves."""
    project = _create(door, "alice").json()["project_id"]
    granted = door.as_("alice").put(f"/projects/{project}/members", json={"user": "keycloak~bob", "relation": "annotator"})
    door.as_("alice").put(f"/projects/{project}/members", json={"user": "keycloak~rex", "relation": "annotator"})
    _task(door, project, "t1", state="unassigned", assignee=None)
    _task(door, project, "t2", state="unassigned", assignee=None)
    door.as_("bob").get("/tasks/t1")
    before_signing_in = door.as_("alice").get(f"/projects/{project}/members").json()["people"]

    door.as_("bob").get("/me")
    door.as_("rex").get("/me")
    signed_in = door.as_("alice").get(f"/projects/{project}/members").json()["people"]
    claimed = door.as_("bob").post("/tasks/t1/events", json={"event": "claim"})
    saved = _save(door, "bob", project, inserts=[{"id": "b1", "shape_type": "bbox", "label": "a"}])
    door.as_("rex").post("/tasks/t2/events", json={"event": "claim"})

    assert granted.json()["people"] == before_signing_in == {"keycloak~alice": "Alice Ek"}, "a read other than /me keeps no name"
    assert signed_in == {"keycloak~alice": "Alice Ek", "keycloak~bob": "bob"}, "signing in registers the name the token carries"
    assert (claimed.status_code, saved.status_code) == (200, 200), saved.text
    history = door.as_("alice").get(f"/annotations/{UNIT}/versions", params={"project": project, "task": "t1", "dataset": DATASET}).json()
    assert [(v["author"], v["author_name"]) for v in history] == [("keycloak~bob", "bob")]
    assert door.as_("alice").get(f"/projects/{project}/members").json()["people"] == {"keycloak~alice": "Alice Ek", "keycloak~bob": "bob"}
    assert door.as_("alice").get("/tasks/t2").json()["people"] == {}
