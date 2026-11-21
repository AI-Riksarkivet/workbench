"""the task and project routes publish their declared models, never a stored document as it lies.

With no response model, `/openapi.json` describes a response as "an object", and whatever the stored document holds is
what ships: a field added to a document for the store's own bookkeeping, or one that drifted, reaches every client.
The response models are the DOMAIN models where one describes the payload — `Task`, `Draft`, `AnnotationProject` — so
the declaration cannot drift from what the store holds. Driven over a real state store (`state_app`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

import psycopg
import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as events_ep
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import AnnotationProject, ProjectState, Shape, Task, TaskState, Transition
from tests.postgres import StateDatabase


#: Far enough ahead that the claim is held whenever the test runs: a lapsed claim reads as released.
LEASE = datetime(2100, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _full_task() -> Task:
    """A task with EVERY optional field populated — an empty document proves nothing about filtering."""
    return Task(
        task_id="g1-r1",
        project_id="p1",
        state=TaskState.CLAIMED,
        assignee="dana",
        lease_expires_at=LEASE,
        source=cast(Any, {"kind": "chunks", "keys": ["t1"]}),
        media=cast(Any, {"kind": "image"}),
        submitted_by="dana",
        submitted_at=NOW,
        reviewed_by="rae",
        reviewed_at=NOW,
        review_action="accepted",
        replica_of="g1",
        skipped_reason="unreadable",
        lead_time_seconds=12.5,
        prediction=[Shape(shape_id="s1", shape_type=cast(Any, "bbox"), label="letter", source="model")],
        transitions=[Transition(at=NOW, by="dana", event="claim", from_state=TaskState.UNASSIGNED, to_state=TaskState.CLAIMED)],
    )


@pytest.fixture
def client(state_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    """The task and project routers over a store holding project p1 and its full task g1-r1, as dana."""

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        return True

    app = state_app(tasks_ep.router, events_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "dana"
    with TestClient(app) as c:
        store = app.state.store
        _portal(c).call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="charters", state=ProjectState.LABELING))
        _portal(c).call(store.send_tasks, "p1", [_full_task()])
        yield c


def _portal(client: TestClient) -> BlockingPortal:
    """The running app's loop, through which the store is reached."""
    assert client.portal is not None, "the client is not running the app"
    return client.portal


def test_a_draft_read_publishes_the_draft_model_or_null_before_the_first(client: TestClient) -> None:
    before = client.get("/tasks/g1-r1/draft")
    app: Any = client.app
    shape = Shape(shape_id="s1", shape_type=cast(Any, "bbox"), label="letter", source="human")
    saved = _portal(client).call(partial(app.state.store.change_draft, "g1-r1", author="dana", change=lambda _current, _now: ([shape], []), base_revision=0))

    after = client.get("/tasks/g1-r1/draft")

    assert (before.status_code, before.json()) == (200, None)
    assert (after.status_code, after.json()) == (200, saved.model_dump(mode="json"))


def test_a_stored_document_that_has_drifted_does_not_reach_the_client(client: TestClient, state_database: StateDatabase) -> None:
    """The finding itself: whatever the document holds is what shipped. The drift is written past the store, into the
    row, as a replica on other code would leave it."""
    drift = {"internal_scratch": {"bookkeeping": True}, "operator_note": "do not ship"}
    with psycopg.connect(host=state_database.host, port=state_database.port, user=state_database.user, dbname=state_database.database) as conn:
        conn.execute("UPDATE tasks SET document = document || %s WHERE task_id = 'g1-r1'", (Jsonb(drift),))

    body = client.get("/tasks/g1-r1").json()

    events = [
        {"event": e, "to": t, "permission": p}
        for e, t, p in [
            ("release", "unassigned", "can_annotate"),
            ("save_draft", "claimed", "can_annotate"),
            ("skip", "skipped", "can_annotate"),
            ("submit", "in_review", "can_annotate"),
        ]
    ]
    sent_at = body.pop("sent_at")
    assert body == {**_full_task().model_dump(mode="json", exclude={"sent_at"}), "legal_events": events, "people": {}}
    assert isinstance(sent_at, str), "the send stamps when the item was sent"


def test_the_plain_task_listing_publishes_its_four_keys_and_no_details(client: TestClient) -> None:
    """`details`/`missing` are `v.optional(...)` on the far side of the wire — valibot's optional accepts UNDEFINED, not
    null. Publishing them as `null` on the plain listing (which a declared response model does by default) would fail
    the app's boundary parse for every queue read."""
    body = client.get("/projects/p1/tasks").json()

    assert body == {
        "counts": {**dict.fromkeys((s.value for s in TaskState), 0), "claimed": 1},
        "total": 1,
        "terminal": 0,
        "may_publish": False,
    }
