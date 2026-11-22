"""Project lifecycle endpoints, publish among them, over the real state store.

A publish asks the workbench's own `can_publish` on the project and nothing of the lakehouse: the publish writes as the
workbench's own identity, and the lakehouse decides that write itself. The project and its tasks are rows in a
PostgreSQL database of the test's own; the checker answers the relations a test grants, and records what was asked.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.projects.models import AnnotationProject, ProjectState, Task, TaskState
from annotator.state.store import AnnotatorStore
from tests.media_catalog import lakehouse_dir
from workbench.media.deps import get_state
from workbench.media.state import AppState


SUBJECT = "henry"
TARGET = "acme$published"


class _Door:
    """One app over the test's database: its client, what the checker was asked, and the store to seed and read."""

    def __init__(self, client: TestClient, app: FastAPI, seen: list[dict[str, Any]]) -> None:
        self.client, self._app, self.seen = client, app, seen

    def store(self, call: Callable[[AnnotatorStore], Any]) -> Any:
        """Run ``call`` against the app's store, on the app's own event loop."""
        store: AnnotatorStore = self._app.state.store

        async def run() -> Any:
            return await call(store)

        portal = self.client.portal
        assert portal is not None, "the client runs no app"
        return portal.call(run)

    def project(self, project_id: str = "p1") -> AnnotationProject | None:
        return self.store(lambda s: s.get_project(project_id))

    def task(self, task_id: str) -> Task:
        found = self.store(lambda s: s.get_task(task_id))
        assert found is not None, f"task {task_id} was not stored"
        return found


def _task(task_id: str, project_id: str = "p1", **fields: Any) -> Task:
    return Task.model_validate(
        {"task_id": task_id, "project_id": project_id, "source": {"kind": "chunks", "keys": [task_id]}, "media": {"kind": "image"}, **fields}
    )


async def _drive(store: AnnotatorStore, task_id: str, state: TaskState, annotator: str = "gina") -> None:
    """Take an unassigned task to ``state`` through the transitions a person would fire."""
    if state is TaskState.UNASSIGNED:
        return
    await store.fire_task(task_id, event="claim", actor=annotator)
    if state is TaskState.CLAIMED:
        return
    if state is TaskState.SKIPPED:
        await store.fire_task(task_id, event="skip", actor=annotator)
        return
    await store.fire_task(task_id, event="submit", actor=annotator)
    if state is TaskState.ACCEPTED:
        await store.fire_task(task_id, event="accept", actor="carol")


#: The project transitions after `labeling` that take a project to each state a test asks for.
_TO: dict[ProjectState, list[str]] = {
    ProjectState.LABELING: [],
    ProjectState.FROZEN: ["freeze"],
    ProjectState.PUBLISH_FAILED: ["freeze", "publish", "publish_failed"],
    ProjectState.PUBLISHED: ["freeze", "publish", "publish_succeeded"],
}


def _seed(door: _Door, state: ProjectState, *, project_id: str = "p1", tasks: dict[str, TaskState] | None = None, **fields: Any) -> None:
    """A project in ``state`` holding the given tasks (one accepted task by default), driven there as people would."""
    # A task id is unique across projects, so a second project's tasks carry its id.
    default = {"t0" if project_id == "p1" else f"{project_id}-t0": TaskState.ACCEPTED}
    held = default if tasks is None else tasks

    async def seed(store: AnnotatorStore) -> None:
        await store.create_project(
            AnnotationProject.model_validate({"project_id": project_id, "tenant": "acme", "slug": "charters", "state": "labeling", **fields})
        )
        await store.send_tasks(project_id, [_task(task_id, project_id) for task_id in held])
        for task_id, task_state in held.items():
            await _drive(store, task_id, task_state)
        for event in _TO[state]:
            await store.fire_project(project_id, event=event, actor="meg", target_namespace=TARGET)

    door.store(seed)


@pytest.fixture
def wired(state_app: Callable[..., FastAPI], tmp_path: Path) -> Iterator[Callable[..., _Door]]:
    """Build the project-events door granting the given relations; each build is its own app on the test's database."""
    with ExitStack() as stack:

        def build(grant: set[str]) -> _Door:
            seen: list[dict[str, Any]] = []

            async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
                seen.append({"user": user, "relation": relation, "obj": obj})
                # Every role views the project (`model.fga`: each rung implies `viewer`).
                return relation in grant or (relation == "can_view" and bool(grant))

            app = state_app(ev.router)
            app.dependency_overrides[get_checker] = lambda: checker
            app.dependency_overrides[get_authorizer] = lambda: None
            app.dependency_overrides[current_subject] = lambda: SUBJECT
            media = AppState(settings=AnnotatorSettings.model_validate({**lakehouse_dir(str(tmp_path))}))
            app.dependency_overrides[get_state] = lambda: media
            client = stack.enter_context(TestClient(app))
            return _Door(client, app, seen)

        yield build


ALL_PUBLISH_DOORS = {"can_publish"}


# --------------------------------------------------------------------------------------------------
# The publish doors — §6.2
# --------------------------------------------------------------------------------------------------


def test_publish_asks_the_projects_own_role_and_nothing_of_the_lakehouse(wired: Any) -> None:
    door = wired(ALL_PUBLISH_DOORS)
    _seed(door, ProjectState.FROZEN)

    r = door.client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 200, r.text
    assert r.json()["state"] == "publishing"
    assert [(c["relation"], c["obj"]) for c in door.seen] == [("can_view", "project:p1"), ("can_publish", "project:p1")]


def test_with_no_lakehouse_declared_a_publish_says_there_is_nowhere_to_publish_and_the_project_stays(
    wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labeling needs no lakehouse and a publish does: a deployment that declares none refuses the publish in plain
    words before the project moves, so its work stays as it is."""
    door = wired(ALL_PUBLISH_DOORS)
    _seed(door, ProjectState.FROZEN)
    monkeypatch.setenv("WORKBENCH_SOURCES", json.dumps([{"kind": "namespace", "name": "scans", "impl": "dir", "properties": {"root": str(tmp_path)}}]))
    get_annotator_settings.cache_clear()

    r = door.client.post("/projects/p1/events", json={"event": "publish"})

    project = door.project()
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "annotation project p1 cannot be published: this deployment reads no rask lakehouse, so there is nowhere to publish to. "
        "Its work stays here until a lakehouse is declared among the sources."
    )
    assert project is not None and project.state is ProjectState.FROZEN


@pytest.mark.parametrize(
    ("named", "status", "pinned"),
    [
        pytest.param(None, 200, TARGET, id="none-named-is-the-default-of-its-warehouse"),
        pytest.param("acme$labels$v2", 200, "acme$labels$v2", id="a-namespace-of-its-warehouse"),
        pytest.param("labels", 400, None, id="a-bare-name-is-no-warehouse"),
        pytest.param("other$labels", 400, None, id="another-warehouse"),
    ],
)
def test_a_publish_lands_in_its_projects_own_warehouse(wired: Any, named: str | None, status: int, pinned: str | None) -> None:
    """The target pinned with the token is a namespace of the project's own warehouse: the default one when the request
    names none, the one it names when that is inside it, and anything else is refused before the project moves."""
    door = wired(ALL_PUBLISH_DOORS)
    _seed(door, ProjectState.FROZEN)

    r = door.client.post("/projects/p1/events", json={"event": "publish", **({"target_namespace": named} if named else {})})

    project = door.project()
    assert r.status_code == status, r.text
    assert project is not None and project.pending_target_namespace == pinned
    if pinned is None:
        assert project.state is ProjectState.FROZEN


def test_publish_without_the_projects_publish_role_is_403(wired: Any) -> None:
    door = wired({"can_view"})
    _seed(door, ProjectState.FROZEN)

    r = door.client.post("/projects/p1/events", json={"event": "publish"})

    project = door.project()
    assert r.status_code == 403
    assert "can_publish" in r.json()["detail"], "the refusal must name the role that is missing"
    assert project is not None and (project.state, project.pending_publish_id) == (ProjectState.FROZEN, None)


def test_a_store_precondition_failure_is_409_not_500(wired: Any) -> None:
    """Every task terminal is checked by the store's `publish` transition, so it holds for any caller; the door
    surfaces it as a conflict, not an error."""
    door = wired(ALL_PUBLISH_DOORS)
    _seed(door, ProjectState.FROZEN, tasks={"t0": TaskState.ACCEPTED, "t1": TaskState.UNASSIGNED})

    r = door.client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 409
    assert "not all terminal" in r.json()["detail"]


# --------------------------------------------------------------------------------------------------
# The other edges read their permission from the machine
# --------------------------------------------------------------------------------------------------


def test_a_non_publish_edge_checks_the_permission_the_table_declares(wired: Any) -> None:
    door = wired({"can_manage"})
    _seed(door, ProjectState.FROZEN)

    r = door.client.post("/projects/p1/events", json={"event": "open"})

    assert r.status_code == 200, r.text
    assert door.seen == [{"user": SUBJECT, "relation": rel, "obj": "project:p1"} for rel in ("can_view", "can_manage")]
    project = door.project()
    assert project is not None and project.state is ProjectState.LABELING


@pytest.mark.parametrize("state", [pytest.param(ProjectState.FROZEN, id="illegal-edge"), pytest.param(None, id="absent-project")])
def test_an_illegal_or_absent_target_is_409_to_a_viewer(wired: Any, state: ProjectState | None) -> None:
    door = wired({"can_view"})
    if state is not None:
        _seed(door, state)

    r = door.client.post("/projects/p1/events", json={"event": "freeze"})

    assert r.status_code == 409, r.text
    project = door.project()
    assert project is None or project.state is ProjectState.FROZEN


def test_a_stranger_learns_nothing_of_a_project_from_its_events(wired: Any) -> None:
    """Which relation an event needs, and whether it is legal, depend on the project's state: to someone who may not view
    the project, every state, an absent project and one that names no publish warehouse answer the same refusal for
    every event, naming no relation and no state — only the id the caller sent."""
    door = wired(set())
    projects = {"p-labeling": ProjectState.LABELING, "p-frozen": ProjectState.FROZEN, "p-failed": ProjectState.PUBLISH_FAILED}
    for project_id, state in projects.items():
        _seed(door, state, project_id=project_id)
    _seed(door, ProjectState.FROZEN, project_id="p-nowhere", tenant="", warehouse="")

    answers = set()
    for project_id in [*projects, "p-nowhere", "p-absent"]:
        for event in ("freeze", "publish", "publish_succeeded"):
            r = door.client.post(f"/projects/{project_id}/events", json={"event": event})
            answers.add((r.status_code, r.content.replace(project_id.encode(), b"<id>")))

    assert len(answers) == 1
    status, body = answers.pop()
    assert (status, json.loads(body)["detail"]) == (404, "No project <id> that you can see")
    assert {project_id: (p.state if (p := door.project(project_id)) else None) for project_id in projects} == projects


@pytest.mark.parametrize("event", ["publish_succeeded", "publish_failed"])
def test_a_system_only_project_edge_is_refused_over_http(wired: Any, event: str) -> None:
    """These are fired by the publish saga. Exposed, anyone could mark another operator's publish succeeded, or fail
    one that was mid-flight."""
    door = wired({"can_manage", "can_publish"})
    _seed(door, ProjectState.FROZEN)
    door.store(lambda s: s.fire_project("p1", event="publish", actor="meg", target_namespace=TARGET))

    r = door.client.post("/projects/p1/events", json={"event": event})

    project = door.project()
    assert r.status_code == 403
    assert project is not None and project.state is ProjectState.PUBLISHING


# --------------------------------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------------------------------


def _item(task_id: str, **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "source": {"kind": "chunks", "keys": [task_id]}, "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"}, **extra}


@pytest.mark.parametrize(
    "state", [pytest.param(ProjectState.LABELING, id="open"), pytest.param(ProjectState.FROZEN, id="frozen"), pytest.param(None, id="absent")]
)
def test_send_without_the_permission_is_404_whatever_the_project_is_and_writes_nothing(wired: Any, state: ProjectState | None) -> None:
    """The roles are asked before the project is read, so a refusal says nothing of whether it exists or which state it is in."""
    door = wired(set())
    if state is not None:
        _seed(door, state, tasks={})

    r = door.client.post("/projects/p1/items", json={"items": [_item("t9")]})

    assert (r.status_code, r.json()["detail"]) == (404, "No project p1 that you can see")
    assert door.store(lambda s: s.get_task("t9")) is None


def test_a_sender_cannot_fabricate_state_or_provenance(wired: Any) -> None:
    """The request model accepts only what to annotate; every provenance field takes its server-side default, so a sender
    cannot manufacture reviewed work. An item stores no media link either: the viewer signs a task's picture from its key."""
    door = wired({"can_send_items"})
    _seed(door, ProjectState.LABELING, tasks={})

    r = door.client.post(
        "/projects/p1/items",
        json={"items": [_item("t9", state="accepted", submitted_by="mallory", reviewed_by="mallory", review_action="accepted")]},
    )

    stored = door.task("t9")
    assert r.status_code == 201, r.text
    assert (stored.state, stored.submitted_by, stored.reviewed_by, stored.review_action) == (TaskState.UNASSIGNED, None, None, None)
    assert stored.media.model_dump() == {"kind": "image", "width": None, "height": None}


def test_send_captures_review_required_and_the_lease_from_the_project(wired: Any) -> None:
    """Captured at send: `submit` decides its target from the task's copy, and reading it per request would put it back
    in the caller's hands."""
    door = wired({"can_send_items"})
    _seed(door, ProjectState.LABELING, tasks={}, review_required=False, lease_seconds=900)

    door.client.post("/projects/p1/items", json={"items": [_item("t9")]})

    stored = door.task("t9")
    assert (stored.review_required, stored.lease_seconds) == (False, 900)


# --------------------------------------------------------------------------------------------------
# The details listing — what A2's queue renders
# --------------------------------------------------------------------------------------------------


def test_details_listing_carries_each_tasks_document_and_what_the_caller_may_fire_on_it(wired: Any) -> None:
    """The plain listing is task id → state; the queue needs assignee, lease and media and the ACTIONS: each task's
    `legal_events` is what THIS caller may fire on it now, the edges out of its state that their role allows and no
    identity rule refuses them. A reviewer who is no manager is offered the verdicts on someone else's submission and
    none on their own, nothing on a claim another annotator holds, the holder's own events on their claim, and the claim
    of an item nobody holds."""
    door = wired({"can_view", "can_claim", "can_annotate", "can_review"})
    _seed(door, ProjectState.LABELING, tasks={"t1": TaskState.CLAIMED, "t2": TaskState.IN_REVIEW, "t3": TaskState.UNASSIGNED})
    door.store(lambda s: s.send_tasks("p1", [_task("t4"), _task("t5")]))
    door.store(lambda s: _drive(s, "t4", TaskState.IN_REVIEW, annotator=SUBJECT))
    door.store(lambda s: _drive(s, "t5", TaskState.CLAIMED, annotator=SUBJECT))

    r = door.client.get("/projects/p1/tasks", params={"include": "details"})

    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {d["task_id"]: d for d in body["details"]}
    assert (list(by_id), body["missing"]) == (["t1", "t2", "t3", "t4", "t5"], [])
    assert (by_id["t1"]["assignee"], by_id["t2"]["submitted_by"], by_id["t4"]["submitted_by"]) == ("gina", "gina", SUBJECT)
    assert {task_id: sorted(e["event"] for e in d["legal_events"]) for task_id, d in by_id.items()} == {
        "t1": [],
        "t2": ["accept", "fix_and_accept", "request_changes"],
        "t3": ["claim"],
        "t4": [],
        "t5": ["release", "save_draft", "skip", "submit"],
    }


def test_details_on_a_published_project_carry_no_task_events(wired: Any) -> None:
    """Rule 5: nothing escapes a published project. The tasks' own states still admit edges (accepted → reopen), but
    the project refuses them all, so the listing hands the UI no action that can only 409."""
    door = wired({"can_view"})
    _seed(door, ProjectState.PUBLISHED)

    r = door.client.get("/projects/p1/tasks", params={"include": "details"})

    assert r.status_code == 200, r.text
    assert [(d["task_id"], d["legal_events"]) for d in r.json()["details"]] == [("t0", [])]


# --------------------------------------------------------------------------------------------------
# Consensus v1 — send writes N independent replicas per item
# --------------------------------------------------------------------------------------------------


def test_send_with_consensus_writes_n_replicas_per_item(wired: Any) -> None:
    """Deterministic sibling ids (`{gid}-r{k}`) are what lets the one-replica-per-annotator guard find the siblings."""
    door = wired({"can_send_items"})
    _seed(door, ProjectState.LABELING, tasks={}, consensus_n=3)

    r = door.client.post("/projects/p1/items", json={"items": [{"task_id": "item1", "source": {"kind": "chunks", "keys": ["k1"]}, "media": {"kind": "image"}}]})

    stored = door.store(lambda s: s.get_tasks("p1", ["item1-r1", "item1-r2", "item1-r3"]))
    assert r.status_code == 201, r.text
    assert (r.json()["sent"], r.json()["created"]) == (1, 3)
    assert [(t.task_id, t.replica_of, t.source.keys) for t in stored] == [(f"item1-r{k}", "item1", ["k1"]) for k in (1, 2, 3)]


def test_send_refuses_when_items_times_consensus_would_exceed_the_cap(wired: Any) -> None:
    """334 items × consensus_n=3 = 1002 tasks, over the 1000-task cap a plain send is held to."""
    door = wired({"can_send_items"})
    _seed(door, ProjectState.LABELING, tasks={}, consensus_n=3)
    items = [{"source": {"kind": "chunks", "keys": [f"k{i}"]}, "media": {"kind": "image"}} for i in range(334)]

    r = door.client.post("/projects/p1/items", json={"items": items})

    assert r.status_code == 409, r.text
    assert "exceeds the 1000-task send cap" in r.json()["detail"]
    assert door.store(lambda s: s.task_index("p1")).total == 0, "a refused send must write NOTHING"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — the adjudication doors (a manager's pick)
# --------------------------------------------------------------------------------------------------


def _replicas(door: _Door, **states: TaskState) -> None:
    """A labeling project holding replicas of group g1 (`r1` is `g1-r1`), each worked by an annotator of its own."""

    async def seed(store: AnnotatorStore) -> None:
        await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="charters", state=ProjectState.LABELING))
        await store.send_tasks("p1", [_task(f"g1-{name}", replica_of="g1") for name in states])
        for i, (name, state) in enumerate(states.items()):
            await _drive(store, f"g1-{name}", state, annotator=f"annotator{i}")

    door.store(seed)


def test_adjudicate_is_gated_on_can_manage_and_records_the_pick(wired: Any) -> None:
    """Adjudication decides which OPINION wins — the manager's authority, not a review action."""
    door = wired({"can_manage"})
    _replicas(door, r1=TaskState.ACCEPTED, r2=TaskState.ACCEPTED)

    r = door.client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r2"})

    assert r.status_code == 200, r.text
    pick = r.json()["adjudications"]["g1"]
    assert (pick["task_id"], pick["by"]) == ("g1-r2", SUBJECT)
    project = door.project()
    assert project is not None and project.adjudications["g1"].task_id == "g1-r2"
    assert ("can_manage", "project:p1") in [(c["relation"], c["obj"]) for c in door.seen]


def test_adjudicate_without_can_manage_is_403(wired: Any) -> None:
    door = wired({"can_review"})
    _replicas(door, r1=TaskState.ACCEPTED)

    r = door.client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r1"})

    assert r.status_code == 403, r.text
    assert "can_manage" in r.json()["detail"]
    project = door.project()
    assert project is not None and project.adjudications == {}


def test_adjudicate_surfaces_the_stores_refusal_as_409(wired: Any) -> None:
    door = wired({"can_manage"})
    _replicas(door, r1=TaskState.SKIPPED)

    r = door.client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r1"})

    assert r.status_code == 409, r.text
    assert "not accepted" in r.json()["detail"]


def test_clearing_an_adjudication_is_a_manager_gated_delete(wired: Any) -> None:
    door = wired({"can_manage"})
    _replicas(door, r1=TaskState.ACCEPTED)
    door.store(lambda s: s.adjudicate("p1", group="g1", task_id="g1-r1", actor="meg"))

    r = door.client.delete("/projects/p1/adjudications/g1")

    assert r.status_code == 200, r.text
    assert r.json()["adjudications"] == {}
    project = door.project()
    assert project is not None and project.adjudications == {}
    assert ("can_manage", "project:p1") in [(c["relation"], c["obj"]) for c in door.seen]
