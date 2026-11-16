"""Importing annotations must not freeze the annotator's event loop.

open_fastapi-audit — "`POST /tasks/{task_id}/import` decodes a caller-supplied Arrow IPC payload and
validates every row through Pydantic inline on the event loop".

`import_annotations` is `async def`, and correctly so: it awaits the state store. But `shapes_from_ipc` is fully
synchronous CPU work over the entire request body — the Arrow decode and its buffer validation, `to_pylist()`,
per-row Pydantic construction and a second pass for the ontology check — and none of it yields. The annotator process
has one event loop, so run inline it would freeze every other in-flight request and the `/livez` + `/readyz` probes,
for as long as a caller-sized upload takes to parse. The decode rides the threadpool.

MEASURED BY ELAPSED TIME, not by a tick count. Counting ticks would pass either way — a blocking decode simply runs
first and the ticker finishes afterwards, still reaching its total. Only the wall clock separates "concurrent" from
"serialized". The request and the ticker run on the app's own loop (`client.portal`), where the state store's pool
lives.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task, TaskState


SUBJECT = "gina"

#: Long enough that a serialized run is unambiguous against scheduler noise, short enough that the
#: test stays cheap. The ticker below runs for ~0.20 s alongside it.
DECODE_SECONDS = 0.30


def test_the_arrow_decode_does_not_serialize_the_loop(state_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing one: other coroutines must keep running during the decode."""

    def _slow_decode(payload: bytes, *, max_bytes: int, ontology: Any = None, taken_ids: Any = None) -> tuple[list, list]:  # noqa: ARG001
        """Stands in for the real pyarrow decode + two Pydantic passes. Sleeps, like they compute."""
        time.sleep(DECODE_SECONDS)
        return [], []

    monkeypatch.setattr(tasks_ep, "shapes_from_ipc", _slow_decode)

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:  # noqa: ARG001
        return True

    app = state_app(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: SUBJECT

    with TestClient(app) as client:
        store, portal = app.state.store, client.portal
        assert portal is not None
        portal.call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="charters", state=ProjectState.LABELING))
        task = Task(
            task_id="t1",
            project_id="p1",
            source=ItemSource(kind="chunks", keys=["t1"]),
            media=MediaRef(kind="image"),
            state=TaskState.CLAIMED,
            assignee=SUBJECT,
        )
        portal.call(store.send_tasks, "p1", [task])

        async def _race() -> tuple[httpx.Response, int, float]:
            ticks = 0

            async def _ticker() -> None:
                nonlocal ticks
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    ticks += 1

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ann") as http:
                started = time.perf_counter()
                response, _ = await asyncio.gather(http.post("/tasks/t1/import", content=b"arrow-ipc-bytes"), _ticker())
                return response, ticks, time.perf_counter() - started

        response, ticks, elapsed = portal.call(_race)

    assert response.status_code == 200, response.text
    assert ticks == 20
    assert elapsed < DECODE_SECONDS + 0.15, (
        f"the import serialized with the ticker ({elapsed:.2f}s vs a {DECODE_SECONDS:.2f}s decode) — "
        f"the Arrow decode ran ON the event loop, freezing every other request and the pod's probes"
    )
