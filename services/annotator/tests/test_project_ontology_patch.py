"""`PATCH /projects/{id}/ontology` — the task definition is editable, and gated on three axes.

The gates each exist for a different reason:

* **Permission.** Editing the definition changes what every FUTURE item promises downstream, so it
  is a `can_manage` act, not a labeling one — the same relation `open`/`freeze`/`publish` carry.
* **State.** Past `labeling` the answer set is closed and a publish is being prepared against it, so
  an edit could only be ignored (every remaining item already captured its copy) or misleading —
  the run facet would report a taxonomy no task was ever judged against.
* **Shape.** The document is validated as a UNIT, so a contradictory edit is refused rather than
  half-applied.

Driven over the real state store; what each test reads back is the stored project.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.projects import router
from annotator.projects.models import AnnotationProject, ProjectState
from annotator.state.store import AnnotatorStore


BBOX = {"kind": "object-detection", "classes": [{"name": "region", "tools": ["bbox"]}]}


class _Door:
    def __init__(self, client: TestClient, app: FastAPI) -> None:
        self.client, self._app = client, app

    def store(self, call: Callable[[AnnotatorStore], Any]) -> Any:
        store: AnnotatorStore = self._app.state.store

        async def run() -> Any:
            return await call(store)

        portal = self.client.portal
        assert portal is not None, "the client runs no app"
        return portal.call(run)

    def seed(self, state: ProjectState = ProjectState.DRAFT, **fields: Any) -> None:
        self.store(
            lambda s: s.create_project(AnnotationProject.model_validate({"project_id": "p1", "tenant": "acme", "slug": "vasa", "state": state, **fields}))
        )

    def classes(self) -> list[str]:
        project = self.store(lambda s: s.get_project("p1"))
        assert project is not None
        return [c.name for c in project.ontology.classes]


@pytest.fixture
def door(state_app: Callable[..., FastAPI]) -> Iterator[Callable[..., _Door]]:
    clients: list[TestClient] = []

    def build(*, allow: bool = True, record: list[dict[str, Any]] | None = None) -> _Door:
        async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
            if record is not None:
                record.append({"user": user, "relation": relation, "obj": obj})
            return allow

        app = state_app(router)
        app.dependency_overrides[get_checker] = lambda: checker
        app.dependency_overrides[get_authorizer] = lambda: None
        app.dependency_overrides[current_subject] = lambda: "gina"
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return _Door(client, app)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("state", [ProjectState.DRAFT, ProjectState.LABELING])
def test_a_manager_replaces_the_ontology_while_the_project_takes_work(door: Any, state: ProjectState) -> None:
    """`labeling` is included: items already sent captured their own copy, so work in review is judged by the contract
    it was issued under — which is what makes an edit safe at all."""
    record: list[dict[str, Any]] = []
    d = door(record=record)
    d.seed(state)

    r = d.client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 200, r.text
    assert [c["name"] for c in r.json()["ontology"]["classes"]] == ["region"]
    assert d.classes() == ["region"]
    assert [(c["relation"], c["obj"]) for c in record] == [("can_manage", "project:p1")]


def test_a_denied_caller_gets_403_and_writes_nothing(door: Any) -> None:
    d = door(allow=False)
    d.seed()

    r = d.client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 403, r.text
    assert d.classes() == []


@pytest.mark.parametrize("state", [ProjectState.FROZEN, ProjectState.PUBLISHING])
def test_refused_once_the_answer_set_is_closed(door: Any, state: ProjectState) -> None:
    d = door()
    d.seed(state)

    r = d.client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 409, r.text
    assert state.value in r.json()["detail"], "the refusal must name the state that caused it"
    assert d.classes() == []


def test_a_contradictory_ontology_is_refused_whole(door: Any) -> None:
    """Validated as a unit, so nothing is half-applied: a relation pointing at an undeclared class is refused."""
    d = door()
    d.seed()

    r = d.client.patch("/projects/p1/ontology", json={"ontology": {"classes": [{"name": "key"}], "relations": [{"name": "answers", "to_classes": ["value"]}]}})

    assert r.status_code == 422, r.text
    assert "value" in r.text
    assert d.classes() == []


def test_an_unknown_project_is_404_not_a_silent_create(door: Any) -> None:
    d = door()

    assert d.client.patch("/projects/p1/ontology", json={"ontology": BBOX}).status_code == 404
    assert d.store(lambda s: s.get_project("p1")) is None


def test_the_replace_is_whole_document_so_an_omitted_list_clears_it(door: Any) -> None:
    """A partial merge would have to answer "what does an absent `classes` mean" — cleared, or unchanged? Whole-document
    has one answer, and it is the one the caller can see in the request body."""
    d = door()
    d.seed(ontology=BBOX)

    r = d.client.patch("/projects/p1/ontology", json={"ontology": {"kind": "object-detection"}})

    assert r.status_code == 200, r.text
    assert r.json()["ontology"]["classes"] == []
    assert d.classes() == []
