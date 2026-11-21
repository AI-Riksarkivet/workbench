"""a task document's append-only history is bounded, and what falls off is counted.

`Task.transitions` and `Task.review_notes` are appended on every transition, and the store rewrites the whole
document on every write, so an unbounded trail would make the cost of an event grow with how many came before it.
`claim` → `release` is a legal loop with no rate limit above it, and a review can request changes any number of times.

The history stays — it is the task's audit trail — but it is BOUNDED at the one seam every task write goes through,
and what falls off the end is COUNTED, so the document never claims a trail it does not have. Driven against the real
store on a PostgreSQL database of the test's own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from annotator.core.config import AnnotatorSettings
from annotator.projects.models import (
    MAX_REVIEW_NOTES,
    MAX_TRANSITIONS,
    AnnotationProject,
    ItemSource,
    MediaRef,
    ProjectState,
    ReviewNote,
    Task,
    TaskState,
    Transition,
)
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase


@pytest_asyncio.fixture
async def store(state_settings: AnnotatorSettings) -> AsyncIterator[AnnotatorStore]:
    opened = await AnnotatorStore.open(state_settings)
    await opened.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="charters", state=ProjectState.LABELING))
    yield opened
    await opened.close()


def _task(**kw: Any) -> Task:
    return Task(task_id="t1", project_id="p1", source=ItemSource(kind="chunks", keys=["t1"]), media=MediaRef(kind="image"), **kw)


async def _stored(store: AnnotatorStore) -> Task:
    task = await store.get_task("t1")
    assert task is not None
    return task


@pytest.mark.asyncio
async def test_a_claim_release_loop_does_not_grow_the_document_forever(store: AnnotatorStore) -> None:
    """The loop that has no rate limit above it, run past the cap."""
    await store.send_tasks("p1", [_task()])

    passes = MAX_TRANSITIONS
    for _ in range(passes):
        await store.fire_task("t1", event="claim", actor="dana")
        await store.fire_task("t1", event="release", actor="dana")

    task = await _stored(store)
    assert len(task.transitions) == MAX_TRANSITIONS
    assert task.transitions_dropped == passes * 2 - MAX_TRANSITIONS
    # The tail is what a reader wants — "what just happened" — so the OLDEST rows are the ones to go.
    assert task.transitions[-1].event == "release"


@pytest.mark.asyncio
async def test_an_oversized_document_is_trimmed_by_the_next_write_that_touches_it(store: AnnotatorStore, state_database: StateDatabase) -> None:
    """A row written past the cap (by a build that bounded nothing) converges on the next transition."""
    now = datetime.now(UTC)
    await store.send_tasks("p1", [_task()])
    overfull = _task(
        state=TaskState.UNASSIGNED,
        transitions=[
            Transition(at=now, by="system", event="release", from_state=TaskState.CLAIMED, to_state=TaskState.UNASSIGNED) for _ in range(MAX_TRANSITIONS + 40)
        ],
        review_notes=[ReviewNote(by="rae", at=now, action="request_changes", message=f"note {i}", shape_ids=[]) for i in range(MAX_REVIEW_NOTES + 7)],
    )
    with psycopg.connect(host=state_database.host, port=state_database.port, user=state_database.user, dbname=state_database.database) as conn:
        conn.execute("UPDATE tasks SET document = %s WHERE task_id = 't1'", (Jsonb(overfull.model_dump(mode="json")),))

    await store.fire_task("t1", event="claim", actor="dana")

    task = await _stored(store)
    assert (len(task.transitions), len(task.review_notes)) == (MAX_TRANSITIONS, MAX_REVIEW_NOTES)
    assert task.transitions_dropped == 41, "the claim's own row counts too — 40 over the cap plus the one it just appended"
    assert task.review_notes_dropped == 7


@pytest.mark.asyncio
async def test_a_task_under_the_cap_reports_nothing_dropped(store: AnnotatorStore) -> None:
    """The failure mode that would hide the bound: trimming unconditionally also passes above."""
    await store.send_tasks("p1", [_task()])

    await store.fire_task("t1", event="claim", actor="dana")

    task = await _stored(store)
    assert (len(task.transitions), task.transitions_dropped, task.review_notes_dropped) == (1, 0, 0)
