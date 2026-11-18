"""A project's settings are a manager's to change: `PATCH /projects/{id}/ontology` the task definition, `PATCH
/projects/{id}` the title, description, instructions, review, claim lease and consensus, each gated as below.

The gates each exist for a different reason:

* **Permission.** Editing the definition changes what every FUTURE item promises downstream, so it
  is a `can_manage` act, not a labeling one — the same relation `open`/`freeze`/`publish` carry.
* **State.** Past `labeling` the answer set is closed and a publish is being prepared against it, so
  an edit could only be ignored (every remaining item already captured its copy) or misleading —
  the run facet would report a taxonomy no task was ever judged against.
* **Shape.** The document is validated as a UNIT, so a contradictory edit is refused rather than
  half-applied.

The scalar settings follow the same rules where they apply: review and the claim lease reach the items nobody has
started, like the ontology; the consensus is fixed once a project holds an item (each was made into that many
replicas); and nothing changes once a publish starts.

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
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task, TaskState
from annotator.projects.ontology import MAX_COLOUR
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


def test_an_edit_reaches_every_item_nobody_has_started_and_no_item_someone_has(door: Any) -> None:
    """A project made with no classes (the send dialog's create-and-send) and given them afterwards: its items nobody has
    started take the new ontology, so they can be classed; an item someone claimed (and released) or submitted keeps the
    copy its work was done under."""
    d = door()
    d.seed(ProjectState.LABELING)

    def item(task_id: str, **fields: Any) -> Task:
        return Task.model_validate(
            {"task_id": task_id, "project_id": "p1", "source": ItemSource(kind="chunks", keys=[task_id]), "media": MediaRef(kind="image"), **fields}
        )

    d.store(
        lambda s: s.send_tasks(
            "p1",
            [item("fresh"), item("released", holders=["dave"]), item("submitted", state=TaskState.IN_REVIEW, holders=["dave"], submitted_by="dave")],
        )
    )

    r = d.client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 200, r.text
    classes = {t.task_id: [c.name for c in t.ontology.classes] for t in d.store(lambda s: s.get_tasks("p1", ["fresh", "released", "submitted"]))}
    assert classes == {"fresh": ["region"], "released": [], "submitted": []}


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


def _items(d: _Door, *tasks: tuple[str, dict[str, Any]]) -> None:
    d.store(
        lambda s: s.send_tasks(
            "p1",
            [
                Task.model_validate({"task_id": t, "project_id": "p1", "source": ItemSource(kind="chunks", keys=[t]), "media": MediaRef(kind="image"), **f})
                for t, f in tasks
            ],
        )
    )


def test_a_manager_changes_the_settings_and_review_and_lease_reach_only_the_items_nobody_started(door: Any) -> None:
    d = door()
    d.seed(ProjectState.LABELING)
    _items(d, ("fresh", {}), ("started", {"holders": ["dave"]}))

    r = d.client.patch(
        "/projects/p1",
        json={"title": "Vasa letters", "description": "Seals", "instructions": "Box each seal.", "review_required": False, "lease_seconds": 600},
    )

    assert r.status_code == 200, r.text
    project = d.store(lambda s: s.get_project("p1"))
    assert (project.title, project.description, project.instructions, project.review_required, project.lease_seconds, project.slug) == (
        "Vasa letters",
        "Seals",
        "Box each seal.",
        False,
        600,
        "vasa",
    )
    captured = {t.task_id: (t.review_required, t.lease_seconds) for t in d.store(lambda s: s.get_tasks("p1", ["fresh", "started"]))}
    assert captured == {"fresh": (False, 600), "started": (True, 1800)}


def test_the_consensus_changes_until_the_project_holds_an_item(door: Any) -> None:
    d = door()
    d.seed()

    before = d.client.patch("/projects/p1", json={"consensus_n": 3})
    _items(d, ("fresh", {}))
    after = d.client.patch("/projects/p1", json={"consensus_n": 2})

    assert before.status_code == 200, before.text
    assert after.status_code == 409, after.text
    assert after.json()["detail"] == "this project holds 1 item, each sent as 3 replicas: the consensus is fixed once items are sent"
    assert d.store(lambda s: s.get_project("p1")).consensus_n == 3


@pytest.mark.parametrize(
    ("state", "body", "status"),
    [
        pytest.param(ProjectState.FROZEN, {"title": "renamed"}, 200, id="a-title-while-frozen"),
        pytest.param(ProjectState.FROZEN, {"review_required": False}, 409, id="review-once-work-has-stopped"),
        pytest.param(ProjectState.PUBLISHING, {"title": "renamed"}, 409, id="anything-once-a-publish-starts"),
    ],
)
def test_what_may_change_follows_the_projects_state(door: Any, state: ProjectState, body: dict[str, Any], status: int) -> None:
    d = door()
    d.seed(state)

    r = d.client.patch("/projects/p1", json=body)

    assert r.status_code == status, r.text
    if status == 409:
        assert state.value in r.json()["detail"], "the refusal names the state that caused it"


@pytest.mark.parametrize(
    ("allow", "body", "status"),
    [
        pytest.param(False, {"title": "renamed"}, 403, id="not-a-manager"),
        pytest.param(True, {"title": "x" * 201}, 422, id="an-overlong-title"),
        pytest.param(True, {"slug": "renamed"}, 422, id="a-field-that-is-not-a-setting"),
        pytest.param(True, {"title": "a\x00b"}, 422, id="a-nul-the-store-cannot-keep"),
    ],
)
def test_a_settings_change_the_door_refuses_writes_nothing(door: Any, allow: bool, body: dict[str, Any], status: int) -> None:
    d = door(allow=allow)
    d.seed()

    r = d.client.patch("/projects/p1", json=body)

    assert r.status_code == status, r.text
    project = d.store(lambda s: s.get_project("p1"))
    assert (project.title, project.slug) == ("", "vasa")


def test_text_the_store_cannot_keep_is_refused_naming_the_field(door: Any) -> None:
    """A NUL, or a lone surrogate (which JSON can escape and UTF-8 cannot hold), is a 422 that names the field it is in: a
    top-level setting, and a class name nested two levels into the ontology. So is a class's colour past the bound a
    request is held to, which a stored ontology is not. Nothing is written."""
    d = door()
    d.seed()

    top = d.client.patch("/projects/p1", json={"description": "se\x00als"})
    nul = d.client.patch("/projects/p1/ontology", json={"ontology": {"classes": [{"name": "se\x00al"}]}})
    surrogate = d.client.patch(
        "/projects/p1/ontology", content=b'{"ontology": {"classes": [{"name": "se\\ud800al"}]}}', headers={"content-type": "application/json"}
    )
    colour = d.client.patch("/projects/p1/ontology", json={"ontology": {"classes": [{"name": "seal", "colour": "c" * (MAX_COLOUR + 1)}]}})

    named = [(r.status_code, [e["field"] for e in r.json()["errors"]], r.json()["errors"][0]["message"]) for r in (top, nul, surrogate, colour)]
    assert named == [
        (422, ["body.description"], "Value error, a NUL character (U+0000) cannot be stored"),
        (422, ["body.ontology.classes.0.name"], "Value error, a NUL character (U+0000) cannot be stored"),
        (422, ["body.ontology.classes.0.name"], "Value error, a lone surrogate (U+D800 to U+DFFF) is not text"),
        (422, ["body.ontology.classes.0.colour"], f"Value should have at most {MAX_COLOUR} items after validation, not {MAX_COLOUR + 1}"),
    ]
    assert d.classes() == []
