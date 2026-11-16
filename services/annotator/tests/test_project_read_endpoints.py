"""The projects read surface — what A1's landing and detail pages actually call, over a real state store.

Contracts pinned here:

1. `GET /projects?tenant=` lists the organisation's projects, oldest first, and is gated on membership —
   an org member sees the tenant's projects, an outsider sees a 403, never an empty list that reads as
   "no projects". Another organisation's projects are not among them.
2. `GET /projects/{id}` is `can_view`-gated and carries `legal_events` derived from the MACHINE
   TABLES — the single source the UI renders so it never hardcodes a second copy that drifts — and the per-state
   counts read from its tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task, TaskState


def _project(project_id: str, *, tenant: str = "acme", state: ProjectState = ProjectState.LABELING) -> AnnotationProject:
    """A project as a create makes it; the store stamps its creation on the database's clock, so seeding order is age."""
    return AnnotationProject(project_id=project_id, tenant=tenant, slug=f"slug-{project_id}", state=state)


class _Wired:
    """The router over a real store, with the checker granting `grant` and recording what it was asked."""

    def __init__(self, state_app: Callable[..., FastAPI], grant: set[str]) -> None:
        self.seen: list[dict[str, Any]] = []

        async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
            self.seen.append({"user": user, "relation": relation, "obj": obj})
            # Every role views the project (`model.fga`: each rung implies `viewer`).
            return relation in grant or (relation == "can_view" and bool(grant))

        self.app = state_app(projects_ep.router)
        self.app.dependency_overrides[get_checker] = lambda: checker
        self.app.dependency_overrides[get_authorizer] = lambda: None
        self.app.dependency_overrides[current_subject] = lambda: "gina"

    def seed(self, client: TestClient, *projects: AnnotationProject, tasks: tuple[Task, ...] = ()) -> None:
        store, portal = self.app.state.store, client.portal
        assert portal is not None, "the client is not running the app"
        for project in projects:
            portal.call(store.create_project, project)
        if tasks:
            portal.call(store.send_tasks, tasks[0].project_id, list(tasks))

    def freeze(self, client: TestClient, project_id: str) -> None:
        portal = client.portal
        assert portal is not None, "the client is not running the app"
        portal.call(partial(self.app.state.store.fire_project, project_id, event="freeze", actor="gina"))


@pytest.fixture
def wired(state_app: Callable[..., FastAPI]) -> Callable[[set[str]], _Wired]:
    return lambda grant: _Wired(state_app, grant)


def test_a_member_lists_the_tenants_projects_oldest_first(wired: Callable[[set[str]], _Wired]) -> None:
    w = wired({"can_list_projects"})
    with TestClient(w.app) as client:
        w.seed(client, _project("p1"), _project("elsewhere", tenant="other"), _project("p2", state=ProjectState.FROZEN))

        r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["project_id"], p["state"]) for p in body["projects"]] == [("p1", "labeling"), ("p2", "frozen")]
    assert body["total"] == 2
    assert w.seen[0] == {"user": "gina", "relation": "can_list_projects", "obj": "org:acme"}


def test_a_non_member_gets_403_not_an_empty_list(wired: Callable[[set[str]], _Wired]) -> None:
    w = wired(set())
    with TestClient(w.app) as client:
        w.seed(client, _project("p1"))
        r = client.get("/projects", params={"tenant": "acme"})
    assert r.status_code == 403, "an outsider must be REFUSED — an empty 200 reads as 'no projects'"


def test_get_project_carries_legal_events_from_the_machine_tables_and_its_tasks_counts(wired: Callable[[set[str]], _Wired]) -> None:
    w = wired({"can_view"})
    task = Task(task_id="t1", project_id="p1", source=ItemSource(kind="chunks", keys=["d1"]), media=MediaRef(kind="image"))
    with TestClient(w.app) as client:
        w.seed(client, _project("p1"), tasks=(task,))
        w.freeze(client, "p1")

        r = client.get("/projects/p1")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"]["state"] == "frozen"
    assert body["project"]["counts"] == {**dict.fromkeys((s.value for s in TaskState), 0), "unassigned": 1}
    assert {(e["event"], e["permission"]) for e in body["legal_events"]} == {
        ("open", "can_manage"),
        ("publish", "can_publish"),
        ("archive", "can_manage"),
    }
    assert w.seen[0] == {"user": "gina", "relation": "can_view", "obj": "project:p1"}


def test_a_project_the_reader_may_not_view_reads_as_one_that_does_not_exist(wired: Callable[[set[str]], _Wired]) -> None:
    w = wired(set())
    with TestClient(w.app) as client:
        w.seed(client, _project("p1"))
        r = client.get("/projects/p1")
    assert (r.status_code, r.json()["detail"]) == (404, "No project p1 that you can see")


def test_get_missing_project_is_404(wired: Callable[[set[str]], _Wired]) -> None:
    w = wired({"can_view"})
    with TestClient(w.app) as client:
        assert client.get("/projects/nope").status_code == 404
