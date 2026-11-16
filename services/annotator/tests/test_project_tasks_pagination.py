"""The task listing's details are paged, and `include` is a closed set.

Tasks accumulate one per sent item per consensus replica for the life of a project, so the details listing (each
task's whole document) is a page of at most 200, keyed on the task id so the tail stays reachable. The plain index and
the publish precondition are read over EVERY task whatever page the UI reads: a page that changed `may_publish` would
be a far worse bug than an unpaged listing. Driven over the real state store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.project_events import router
from annotator.projects.models import AnnotationProject, ProjectState, Task
from annotator.state.store import AnnotatorStore


def _seed(total: int) -> Callable[[AnnotatorStore], Any]:
    async def seed(store: AnnotatorStore) -> None:
        await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="vasa", state=ProjectState.LABELING))
        await store.send_tasks(
            "p1",
            [
                Task.model_validate(
                    {"task_id": f"t{i:04d}", "project_id": "p1", "source": {"kind": "chunks", "keys": [f"t{i:04d}"]}, "media": {"kind": "image"}}
                )
                for i in range(total)
            ],
        )
        # One terminal task, so the precondition is false over the whole project while the first page alone holds none.
        await store.fire_task(f"t{total - 1:04d}", event="claim", actor="gina")
        await store.fire_task(f"t{total - 1:04d}", event="skip", actor="gina")

    return seed


@pytest.fixture
def listing(state_app: Callable[..., FastAPI]) -> Iterator[Callable[[int], TestClient]]:
    clients: list[TestClient] = []

    def build(total: int) -> TestClient:
        async def allow(**_kw: object) -> bool:
            return True

        app = state_app(router)
        app.dependency_overrides[get_checker] = lambda: allow
        app.dependency_overrides[get_authorizer] = lambda: None
        app.dependency_overrides[current_subject] = lambda: "gina"
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        seed = _seed(total)
        store: AnnotatorStore = app.state.store

        async def run() -> None:
            await seed(store)

        portal = client.portal
        assert portal is not None, "the client runs no app"
        portal.call(run)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("params", "status"),
    [
        pytest.param({"include": "details", "limit": 200}, 200, id="the-largest-page"),
        pytest.param({"include": "details", "limit": 201}, 422, id="past-the-largest-page"),
        pytest.param({"include": "details", "limit": 0}, 422, id="no-page"),
        pytest.param({"include": "detials"}, 422, id="a-misspelled-include"),
    ],
)
def test_the_page_and_the_include_are_closed_sets(listing: Callable[[int], TestClient], params: dict[str, Any], status: int) -> None:
    """A misspelled `include` is a 422 naming the legal values, never a 200 carrying the plain index."""
    client = listing(3)

    assert client.get("/projects/p1/tasks", params=params).status_code == status


def test_a_page_holds_limit_tasks_and_the_precondition_reads_the_whole_project(listing: Callable[[int], TestClient]) -> None:
    client = listing(250)

    body = client.get("/projects/p1/tasks", params={"include": "details", "limit": 100}).json()

    assert [d["task_id"] for d in body["details"]] == [f"t{i:04d}" for i in range(100)]
    assert body["next_cursor"] == "t0099"
    assert (body["total"], body["terminal"], len(body["tasks"]), body["may_publish"]) == (250, 1, 250, False)


def test_the_cursor_reaches_the_tail(listing: Callable[[int], TestClient]) -> None:
    """A limit with no working cursor hides the rest of the project behind the first page."""
    client = listing(5)
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"include": "details", "limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get("/projects/p1/tasks", params=params).json()
        pages.append([d["task_id"] for d in body["details"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == [["t0000", "t0001"], ["t0002", "t0003"], ["t0004"]]
