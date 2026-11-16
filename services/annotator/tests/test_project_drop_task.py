"""`DELETE /projects/{id}/tasks/{task_id}` — an item that can never be completed must have an exit.

A send can put items into a project that NOBODY can finish. The case that motivated this is an item
naming a media dataset that has since been renamed or removed: the canvas cannot open it, so it
cannot be claimed, submitted or skipped. The publish precondition requires EVERY task terminal, so
one unfinishable item wedges the project permanently — and the only way past it was to abandon the
project and re-send everything.

The same shape as the adjudication clear beside it, and for the same audit-found reason: a state
with no exit is a state that eventually traps someone. Driven over the real state store.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.project_events import router
from annotator.projects.models import AnnotationProject, ProjectState, Task
from annotator.state.store import AnnotatorStore


def _client(state_app: Callable[..., FastAPI], *, allow: bool = True, seen: list[dict[str, Any]] | None = None) -> tuple[TestClient, FastAPI]:
    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = state_app(router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    return TestClient(app), app


def _call(client: TestClient, app: FastAPI, call: Callable[[AnnotatorStore], Any]) -> Any:
    store: AnnotatorStore = app.state.store

    async def run() -> Any:
        return await call(store)

    portal = client.portal
    assert portal is not None, "the client runs no app"
    return portal.call(run)


def _seed(client: TestClient, app: FastAPI, state: ProjectState = ProjectState.LABELING) -> None:
    """Project p1 holding t1 and t2, in ``state``."""

    async def seed(store: AnnotatorStore) -> None:
        await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="vasa", state=ProjectState.LABELING))
        await store.send_tasks(
            "p1",
            [
                Task.model_validate({"task_id": t, "project_id": "p1", "source": {"kind": "chunks", "keys": [t]}, "media": {"kind": "image"}})
                for t in ("t1", "t2")
            ],
        )
        if state is ProjectState.PUBLISHING:
            # Every task terminal first: the `publish` transition requires it.
            for task_id in ("t1", "t2"):
                await store.fire_task(task_id, event="claim", actor="gina")
                await store.fire_task(task_id, event="skip", actor="gina")
        if state is not ProjectState.LABELING:
            await store.fire_project("p1", event="freeze", actor="meg")
        if state is ProjectState.PUBLISHING:
            await store.fire_project("p1", event="publish", actor="meg", target_namespace="acme$published")

    _call(client, app, seed)


def _held(client: TestClient, app: FastAPI) -> list[str]:
    return sorted(_call(client, app, lambda s: s.task_index("p1")).tasks)


def test_a_manager_drops_an_unfinishable_item(state_app: Callable[..., FastAPI]) -> None:
    client, app = _client(state_app)
    with client:
        _seed(client, app)

        r = client.delete("/projects/p1/tasks/t1")

        assert r.status_code == 200, r.text
        assert r.json() == {"task_id": "t1", "removed": True, "total": 1}
        assert _held(client, app) == ["t2"], "dropping one item must remove it and no other"


def test_the_gate_is_can_manage(state_app: Callable[..., FastAPI]) -> None:
    """Discarding someone's queued work is a manager act, not an annotator's."""
    seen: list[dict[str, Any]] = []
    client, app = _client(state_app, seen=seen)
    with client:
        _seed(client, app)

        client.delete("/projects/p1/tasks/t1")

    assert [(c["relation"], c["obj"]) for c in seen] == [("can_manage", "project:p1")]


def test_a_denied_caller_gets_403_and_removes_nothing(state_app: Callable[..., FastAPI]) -> None:
    client, app = _client(state_app, allow=False)
    with client:
        _seed(client, app)

        r = client.delete("/projects/p1/tasks/t1")

        assert r.status_code == 403, r.text
        assert _held(client, app) == ["t1", "t2"]


def test_dropping_an_absent_task_is_a_no_op_not_a_404(state_app: Callable[..., FastAPI]) -> None:
    """Idempotent, so a retry cannot fail against a project that is already how the caller wants it."""
    client, app = _client(state_app)
    with client:
        _seed(client, app)

        r = client.delete("/projects/p1/tasks/never-existed")

        assert r.status_code == 200, r.text
        assert r.json() == {"task_id": "never-existed", "removed": False, "total": 2}


@pytest.mark.parametrize("state", [ProjectState.FROZEN, ProjectState.PUBLISHING])
def test_refused_once_provenance_is_being_prepared(state_app: Callable[..., FastAPI], state: ProjectState) -> None:
    """Past `frozen` the answer set is closed and a publish is being prepared against it. Removing an
    item then would change what the run facet describes after the description was fixed."""
    client, app = _client(state_app)
    with client:
        _seed(client, app, state)

        r = client.delete("/projects/p1/tasks/t1")

        assert r.status_code == 409, r.text
        assert state.value in r.json()["detail"], "the refusal must name the state that caused it"
        assert _held(client, app) == ["t1", "t2"]


def test_an_unknown_project_is_404(state_app: Callable[..., FastAPI]) -> None:
    client, _app = _client(state_app)
    with client:
        assert client.delete("/projects/p1/tasks/t1").status_code == 404
