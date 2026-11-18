"""Bulk labeling: a PREDICTION sent alongside an item (#42, steps 2-3).

Browse could always find keys and queue them. It could not say what they are. So "find five hundred
pages like this one and label them" queued five hundred blank tasks, and every one still had to be
opened and drawn on — the bulk surface was bulk in selection only.

The shape this pins is Label Studio's, deliberately: a PREDICTION is a pre-annotation attached to the
task, NOT the annotator's own work. It rides the task document (one write, at send), it is stamped
server-side, and it is checked against the project's taxonomy before a single task is written. A
human still claims, corrects and submits — nothing here reaches the lakehouse unreviewed, because
nothing can.

Four properties carry the whole design, and a wrong implementation still looks right for all four:
the field must actually SURVIVE the wire (pydantic drops what it does not declare, in silence); the
sender must not be able to forge `source`; an invented label must be refused BEFORE anything is
written; and a prediction must never be a draft, because a draft is submittable and a prediction is
a suggestion. Driven over the real state store, on a dataset in a directory catalog.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.models import MAX_SOURCE_KEYS, AnnotationProject, Draft, ProjectState, Task
from annotator.state.store import AnnotatorStore
from annotator.state.task_query import SortOrder, TaskFilter, TaskSort
from client_kit.media.config import Settings
from client_kit.media.deps import get_state
from client_kit.media.state import AppState
from tests.media_catalog import lakehouse_dir, write_table


SUBJECT = "henry"

#: A two-class taxonomy with tools declared, so both the closed-set check and the per-class tool
#: check have something to refuse. `tag` is the whole-item primitive bulk classification uses.
ONTOLOGY: dict[str, Any] = {
    "classes": [
        {"name": "letter", "tools": ["tag"]},
        {"name": "envelope", "tools": ["tag", "bbox"]},
    ]
}


class _Door:
    def __init__(self, client: TestClient, app: FastAPI) -> None:
        self.client, self._app = client, app

    def call(self, call: Callable[[AnnotatorStore], Any]) -> Any:
        """Run ``call`` against the app's store, on the app's own event loop."""
        store: AnnotatorStore = self._app.state.store
        portal = self.client.portal
        assert portal is not None, "the client runs no app"

        async def run() -> Any:
            return await call(store)

        return portal.call(run)

    def tasks(self) -> list[Task]:
        async def held(store: AnnotatorStore) -> list[Task]:
            return (await store.task_page("p1", TaskFilter(), sort=TaskSort.ITEM, order=SortOrder.ASC, limit=200, offset=0))[2]

        return self.call(held)

    def draft(self, task_id: str) -> Draft | None:
        return self.call(lambda s: s.get_draft(task_id))


@pytest.fixture
def wired(state_app: Callable[..., FastAPI], tmp_path: Path) -> Iterator[Callable[..., _Door]]:
    """The project and the dataset its items name (``acme$vasa``, in a directory catalog)."""
    write_table(tmp_path, ["acme", "vasa", "chunks"], pa.table({"doc_id": ["page-1"]}), descriptor={"identity": {"key_fields": ["doc_id"]}})
    clients: list[TestClient] = []

    def build(*, ontology: dict[str, Any] | None = None, consensus_n: int = 1) -> _Door:
        async def checker(**_call: Any) -> bool:
            """Every relation granted — authorization is `test_project_event_endpoints`'s subject."""
            return True

        app = state_app(ev.router)
        app.dependency_overrides[get_checker] = lambda: checker
        app.dependency_overrides[get_authorizer] = lambda: None
        app.dependency_overrides[current_subject] = lambda: SUBJECT
        state = AppState(settings=Settings.model_validate({**lakehouse_dir(str(tmp_path))}))
        app.dependency_overrides[get_state] = lambda: state
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        door = _Door(client, app)
        project = AnnotationProject.model_validate(
            {
                "project_id": "p1",
                "tenant": "acme",
                "slug": "vasa",
                "state": ProjectState.LABELING,
                "ontology": ONTOLOGY if ontology is None else ontology,
                "consensus_n": consensus_n,
            }
        )
        door.call(lambda s: s.create_project(project))
        return door

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def _item(prediction: list[dict[str, Any]] | None = None, *, task_id: str = "t1") -> dict[str, Any]:
    item: dict[str, Any] = {"task_id": task_id, "source": {"kind": "chunks", "keys": ["page-1"], "where": "lake~acme$vasa"}, "media": {"kind": "image"}}
    if prediction is not None:
        item["prediction"] = prediction
    return item


def test_a_prediction_reaches_the_task_stamped_by_the_server_and_is_no_draft(wired: Any) -> None:
    """`SendItem` must declare `prediction`: pydantic drops an undeclared key without erroring, so a bulk label would
    post 201 and carry nothing. The sender's `source` never lands: the server stamps it. And a prediction is a
    suggestion on the task, never a draft, which is submittable."""
    door = wired()

    r = door.client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter", "source": "human"}])]})

    assert r.status_code == 201, r.text
    [task] = door.tasks()
    assert [(s.label, s.source) for s in task.prediction] == [("letter", "bulk")]
    assert door.draft("t1") is None


def test_an_item_with_no_prediction_carries_none(wired: Any) -> None:
    """The ordinary send is unchanged — an empty list, never a fabricated shape."""
    door = wired()

    r = door.client.post("/projects/p1/items", json={"items": [_item()]})

    assert r.status_code == 201, r.text
    assert [t.prediction for t in door.tasks()] == [[]]


def test_a_tool_the_class_forbids_is_refused(wired: Any) -> None:
    """`letter` declares `tag` only. Membership is not just the label — it is the label WITH the
    primitive it was drawn with."""
    door = wired()

    r = door.client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "bbox", "label": "letter"}])]})

    assert r.status_code == 409, r.text
    assert door.tasks() == []


def test_the_refusal_happens_before_any_item_is_written(wired: Any) -> None:
    """The whole send is validated first: a half-applied bulk action is worse than a refused one, because nobody can
    tell which half landed."""
    door = wired()
    good = _item([{"shape_type": "tag", "label": "letter"}], task_id="t1")
    bad = _item([{"shape_type": "tag", "label": "nope"}], task_id="t2")

    r = door.client.post("/projects/p1/items", json={"items": [good, bad]})

    assert r.status_code == 409, r.text
    assert door.tasks() == [], "the first item was written before the second was validated"


def test_an_unconstrained_project_accepts_any_label(wired: Any) -> None:
    """An empty taxonomy constrains nothing — the same posture every other surface takes."""
    door = wired(ontology={})

    r = door.client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "anything"}])]})

    assert r.status_code == 201, r.text
    assert [s.label for t in door.tasks() for s in t.prediction] == ["anything"]


def test_every_replica_carries_the_same_prediction(wired: Any) -> None:
    """Consensus asks several people the same question. Handing some of them a suggestion and not
    others would make their disagreement an artefact of the send rather than of the images."""
    door = wired(consensus_n=3)

    r = door.client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert r.status_code == 201, r.text
    assert [(t.task_id, [s.label for s in t.prediction]) for t in door.tasks()] == [(f"t1-r{k}", ["letter"]) for k in (1, 2, 3)]


@pytest.mark.parametrize(
    ("item", "field"),
    [
        pytest.param(
            _item([{"shape_type": "tag", "label": "x", "attributes": {"da\x00te": "1701"}}]),
            "body.items.0.prediction.0.attributes.da\x00te.[key]",
            id="a-nul-in-an-attribute-name",
        ),
        pytest.param(_item([{"shape_type": "spaceship", "label": "x"}]), "body.items.0.prediction.0.shape_type", id="a-shape-type-no-shape-has"),
        pytest.param(
            {**_item(), "source": {"kind": "chunks", "keys": [f"page-{i}" for i in range(MAX_SOURCE_KEYS + 1)]}},
            "body.items.0.source.keys",
            id="more-keys-than-a-source-names",
        ),
    ],
)
def test_an_item_the_send_cannot_store_is_refused_naming_its_field(wired: Any, item: dict[str, Any], field: str) -> None:
    """A NUL in a prediction's attribute NAME (a mapping's key, four levels into the body), a shape type no stored shape
    has, or a source past what a sender may name, is a 422 that names its place, and nothing is sent."""
    door = wired(ontology={})

    r = door.client.post("/projects/p1/items", json={"items": [item]})

    assert r.status_code == 422, r.text
    assert [e["field"] for e in r.json()["errors"]] == [field]
    assert door.tasks() == []
