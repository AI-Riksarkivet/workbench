"""The data manager reads a project's items a page at a time: filtered, counted, sorted and paged in Postgres.

Tasks accumulate one per sent item per consensus replica for the life of a project, so the details listing is a page of
at most 200 at a bounded offset, under the filters a person works by (state, assignee, reviewer, submitter, class,
dataset, dates), in a stable order. The plain index and the publish precondition are read over EVERY task whatever the
page: a page that changed `may_publish` would be a far worse bug than an unpaged listing. Driven over the real state
store, with every task moved through the store's own transitions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.project_events import router
from annotator.projects.models import AnnotationProject, Link, ProjectState, Shape, Task
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase


def _task(task_id: str, *, dataset: str = "lake~acme$pages", predicted: str | None = None) -> Task:
    return Task.model_validate(
        {
            "task_id": task_id,
            "project_id": "p1",
            "source": {"kind": "chunks", "keys": [task_id], "where": dataset},
            "media": {"kind": "image"},
            "prediction": [{"shape_type": "bbox", "label": predicted, "source": "model:x"}] if predicted else [],
        }
    )


def _box(label: str) -> Callable[[Any, Any], tuple[list[Shape], list[Any]]]:
    return lambda _current, _now: ([Shape(shape_type=cast(Any, "bbox"), x=1, y=1, width=2, height=2, label=label, source="human")], [])


class _Project:
    """The listing door over a store, as gina, with the store's own transitions to seed it."""

    def __init__(self, client: TestClient, store: AnnotatorStore) -> None:
        self.client, self.store = client, store

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        portal = self.client.portal
        assert portal is not None, "the client runs no app"
        return portal.call(partial(fn, *args, **kwargs))

    def fire(self, task_id: str, *events: tuple[str, str]) -> None:
        for event, actor in events:
            self.call(self.store.fire_task, task_id, event=event, actor=actor)

    def page(self, **params: Any) -> dict[str, Any]:
        answer = self.client.get("/projects/p1/tasks", params={"include": "details", **params})
        assert answer.status_code == 200, answer.text
        return answer.json()

    def ids(self, **params: Any) -> list[str]:
        return [d["task_id"] for d in self.page(**params)["details"]]


@pytest.fixture
def project(state_app: Callable[..., FastAPI]) -> Iterator[_Project]:
    async def allow(**_kw: object) -> bool:
        return True

    app = state_app(router)
    app.dependency_overrides[get_checker] = lambda: allow
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    with TestClient(app) as client:
        store: AnnotatorStore = app.state.store
        seeded = _Project(client, store)
        seeded.call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="vasa", state=ProjectState.LABELING))
        yield seeded


@pytest.fixture
def worked(project: _Project, state_database: StateDatabase) -> _Project:
    """Six items in six states, two datasets, two classes drawn and one proposed, one claim lapsed:

    - a-new: unassigned, proposed `seal` at send;   b-held: claimed by bob, a `stamp` drawn;
    - c-review: in review, ann submitted a `letter`; d-done: accepted, ann submitted a `letter`, rev reviewed it;
    - e-back: sent back to ann by rev;              f-lapsed: bob's claim, past its lease (so unassigned to a reader),
                                                    a box drawn and taken away again, so its draft holds nothing;
    and a-new and f-lapsed come from the `scans` dataset.
    """
    project.call(
        project.store.send_tasks,
        "p1",
        [
            _task("a-new", dataset="lake~acme$scans", predicted="seal"),
            _task("b-held"),
            _task("c-review"),
            _task("d-done"),
            _task("e-back"),
            _task("f-lapsed", dataset="lake~acme$scans"),
        ],
    )
    project.fire("b-held", ("claim", "bob"))
    project.call(project.store.change_draft, "b-held", author="bob", change=_box("stamp"), base_revision=0)
    for task_id in ("c-review", "d-done", "e-back"):
        project.fire(task_id, ("claim", "ann"))
        project.call(project.store.change_draft, task_id, author="ann", change=_box("letter"), base_revision=0)
        project.fire(task_id, ("submit", "ann"))
    project.fire("d-done", ("accept", "rev"))
    project.call(project.store.fire_task, "e-back", event="request_changes", actor="rev", message="redo")
    project.fire("f-lapsed", ("claim", "bob"))
    project.call(project.store.change_draft, "f-lapsed", author="bob", change=_box("stamp"), base_revision=0)
    project.call(project.store.change_draft, "f-lapsed", author="bob", change=lambda _current, _now: ([], []), base_revision=1)
    with psycopg.connect(host=state_database.host, port=state_database.port, user=state_database.user, dbname=state_database.database) as conn:
        conn.execute("UPDATE tasks SET lease_expires_at = now() - interval '1 minute' WHERE task_id = 'f-lapsed'")
    return project


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"limit": 201}, id="past-the-largest-page"),
        pytest.param({"limit": 0}, id="no-page"),
        pytest.param({"offset": 100_001}, id="past-the-deepest-offset"),
        pytest.param({"include": "detials"}, id="a-misspelled-include"),
        pytest.param({"sort": "assignee; drop table tasks"}, id="a-sort-off-the-list"),
        pytest.param({"state": "lost"}, id="a-state-off-the-list"),
        pytest.param({"since": "2026-11-18T10:00:00"}, id="an-instant-with-no-offset"),
        pytest.param({"label": "x" * 513}, id="an-overlong-filter"),
        pytest.param({"holder": "a\x00b"}, id="a-nul-in-a-filter"),
        pytest.param({"around": "\x00"}, id="a-nul-in-around"),
    ],
)
def test_a_page_a_sort_a_filter_and_an_include_off_their_closed_sets_are_refused(project: _Project, params: dict[str, Any]) -> None:
    assert project.client.get("/projects/p1/tasks", params={"include": "details", **params}).status_code == 422


def test_a_page_starts_at_its_offset_and_the_precondition_reads_the_whole_project(project: _Project) -> None:
    project.call(project.store.send_tasks, "p1", [_task(f"t{i:04d}") for i in range(250)])
    # One terminal task past the page, so the precondition is false over the project while the page holds none.
    project.fire("t0249", ("claim", "gina"), ("skip", "gina"))

    body = project.page(limit=100, offset=100)

    assert [d["task_id"] for d in body["details"]] == [f"t{i:04d}" for i in range(100, 200)]
    assert (body["offset"], body["matched"], body["total"], body["terminal"], body["may_publish"]) == (100, 250, 250, 1, False)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"state": ["unassigned"]}, ["a-new", "f-lapsed"], id="a-lapsed-claim-is-unassigned"),
        pytest.param({"state": ["in_review", "accepted"]}, ["c-review", "d-done"], id="several-states"),
        pytest.param({"assignee": "bob"}, ["b-held"], id="the-holder-of-a-live-claim"),
        pytest.param({"submitter": "ann"}, ["c-review", "d-done", "e-back"], id="who-submitted"),
        pytest.param({"reviewer": "rev"}, ["d-done", "e-back"], id="who-reviewed"),
        pytest.param({"holder": "bob"}, ["b-held", "f-lapsed"], id="whose-work-ever"),
        pytest.param({"state": ["in_review"], "not_submitter": "ann"}, [], id="a-reviewer-leaves-out-their-own"),
        pytest.param({"label": "letter"}, ["c-review", "d-done", "e-back"], id="a-class-drawn"),
        pytest.param({"label": "seal"}, ["a-new"], id="a-class-proposed"),
        pytest.param({"dataset": "lake~acme$scans"}, ["a-new", "f-lapsed"], id="a-dataset"),
        pytest.param({"has_work": True}, ["b-held", "c-review", "d-done", "e-back"], id="work-drawn-not-a-claim-alone"),
        pytest.param({"has_work": False}, ["a-new", "f-lapsed"], id="no-work-no-draft-or-one-emptied"),
        pytest.param({"open_to": "bob"}, ["a-new", "b-held", "e-back", "f-lapsed"], id="open-to-a-person-their-claims-and-the-unheld"),
        pytest.param({"open_to": "gina"}, ["a-new", "e-back", "f-lapsed"], id="open-to-a-person-not-anothers-claim"),
    ],
)
def test_a_filter_answers_the_items_it_names(worked: _Project, params: dict[str, Any], expected: list[str]) -> None:
    body = worked.page(**params)

    assert ([d["task_id"] for d in body["details"]], body["matched"]) == (expected, len(expected))
    assert body["datasets"] == ["lake~acme$pages", "lake~acme$scans"], "the filter by dataset offers every dataset of the project"


def test_the_counts_per_state_are_read_under_every_other_filter(worked: _Project) -> None:
    body = worked.page(submitter="ann", state=["accepted"])

    assert {k: v for k, v in body["state_counts"].items() if v} == {"in_review": 1, "accepted": 1, "changes_requested": 1}
    assert body["matched"] == 1


def test_a_date_range_reads_the_instant_it_names(worked: _Project) -> None:
    now = datetime.now(UTC)

    reviewed = worked.ids(date="reviewed", since=(now - timedelta(minutes=5)).isoformat(), until=(now + timedelta(minutes=5)).isoformat())
    before = worked.ids(date="sent", until=(now - timedelta(days=1)).isoformat())

    assert (reviewed, before) == (["d-done", "e-back"], [])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"sort": "state"}, ["a-new", "f-lapsed", "b-held", "e-back", "c-review", "d-done"], id="by-state-in-work-order"),
        pytest.param({"sort": "assignee", "order": "desc"}, ["b-held", "a-new", "c-review", "d-done", "e-back", "f-lapsed"], id="nobody-last"),
        pytest.param({"sort": "item", "order": "desc"}, ["f-lapsed", "e-back", "d-done", "c-review", "b-held", "a-new"], id="by-item"),
    ],
)
def test_a_sort_is_stable_and_puts_what_is_absent_last(worked: _Project, params: dict[str, Any], expected: list[str]) -> None:
    assert worked.ids(**params) == expected


def test_a_page_around_an_item_holds_its_neighbours_and_says_where_it_sits(project: _Project) -> None:
    project.call(project.store.send_tasks, "p1", [_task(f"t{i:04d}") for i in range(50)])

    body = project.page(around="t0030", limit=4)

    assert ([d["task_id"] for d in body["details"]], body["position"], body["offset"]) == (["t0028", "t0029", "t0030", "t0031"], 30, 28)


def _tag(shape_id: str, label: str, **over: Any) -> Shape:
    return Shape.model_validate({"shape_id": shape_id, "shape_type": "tag", "label": label, "source": "human", **over})


def _work(shapes: list[Shape], links: list[Link] | None = None) -> Callable[[Any, Any], tuple[list[Shape], list[Link]]]:
    return lambda _current, _now: (shapes, links or [])


def _plain(tag_id: str, label: str = "letter") -> dict[str, Any]:
    return {"id": tag_id, "label": label, "detailed": False, "prediction": False, "proposed_by": None}


def test_each_items_work_is_answered_as_its_tags_and_revision_where_the_reader_may_read_it(project: _Project) -> None:
    """`include=work`: the revision a change is built on and the item's whole-item tags, by id and class, each saying
    whether it carries more than its class (text, attributes, a link: what a new label would discard) and whether it is a
    proposal no person kept and, for a proposal, the model, version and score that made it; at most 64 an item; a
    replica's work is left out for a reader who may not read it."""

    async def no_review(*, relation: str, **_kw: object) -> bool:
        return relation != "can_review"

    cast(FastAPI, project.client.app).dependency_overrides[get_checker] = lambda: no_review
    replica = _task("g-r1").model_copy(update={"replica_of": "g"})
    project.call(project.store.send_tasks, "p1", [_task("w-tags"), _task("w-none"), _task("w-many"), replica])
    box = Shape(shape_id="a-box", shape_type="bbox", x=1, y=1, width=2, height=2, label="stamp", source="human")
    tagged = [
        _tag("linked", "letter"),
        _tag("said", "map", text="a map of the harbour"),
        _tag("dated", "map", attributes={"year": "1701"}),
        _tag("proposed", "letter", status="prediction", source="model:vlm", model_version="vlm.3", confidence=0.62),
        _tag("plain", "map"),
        box,
        box.model_copy(update={"shape_id": "p-box", "status": "prediction", "source": "model:detector"}),
    ]
    for task_id, change in (
        ("w-tags", _work(tagged, [Link(name="shows", from_shape="a-box", to_shape="linked")])),
        ("w-many", _work([_tag(f"t{i:02d}", "letter") for i in range(65)])),
    ):
        project.fire(task_id, ("claim", "gina"))
        project.call(project.store.change_draft, task_id, author="gina", change=change, base_revision=0)
    project.fire("g-r1", ("claim", "bob"))
    project.call(project.store.change_draft, "g-r1", author="bob", change=_work([_tag("own", "letter")]), base_revision=0)

    body = project.page(include=["details", "work"])

    assert {d["task_id"]: d["work"] for d in body["details"]} == {
        "g-r1": None,
        "w-many": {"revision": 1, "tags": [_plain(f"t{i:02d}") for i in range(64)], "tags_cut": True, "proposals": 0},
        "w-none": {"revision": 0, "tags": [], "tags_cut": False, "proposals": 0},
        "w-tags": {
            "revision": 1,
            "tags": [
                {**_plain("linked"), "detailed": True},
                {**_plain("said", "map"), "detailed": True},
                {**_plain("dated", "map"), "detailed": True},
                {**_plain("proposed"), "prediction": True, "proposed_by": {"model": "vlm", "version": "vlm.3", "score": 0.62}},
                _plain("plain", "map"),
            ],
            "tags_cut": False,
            "proposals": 2,
        },
    }


def test_each_persons_part_is_counted_over_the_whole_project(worked: _Project) -> None:
    body = worked.client.get("/projects/p1/tasks", params={"include": "contributors"}).json()

    assert body["contributors"] == [
        {"principal": "ann", "submitted": 3, "accepted": 1, "changes_requested": 1, "open": 0, "reviewed": 0},
        {"principal": "bob", "submitted": 0, "accepted": 0, "changes_requested": 0, "open": 1, "reviewed": 0},
        {"principal": "rev", "submitted": 0, "accepted": 0, "changes_requested": 0, "open": 0, "reviewed": 2},
    ]
    assert "details" not in body
