"""The annotator's state for its offline tests: a real PostgreSQL database of the test's own.

`state_settings` names the test's database (root `conftest.py::state_database`) in the annotator's settings, the way a
deployment's environment does, so the service's own lifespan (`annotator.main`) opens its store on it. `state_app`
composes a test app from routers with the same store and publish runner, opened in the app's own lifespan: psycopg's
async pool lives on the event loop that opened it, so a test seeds rows through that loop
(`client.portal.call(client.app.state.store.<method>, ...)`), never through one of its own.

`work_client` is the canvas's doors (`annotator.annotations`) and the task doors over that store, on a dataset of a
source outside rask that git marks annotatable, with no lakehouse declared at all: labeling needs none.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from annotator.annotations.router import router as annotations_router
from annotator.api.v1.endpoints import tasks
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.projects.lakehouse import PublishRunner
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, Task, TaskState
from annotator.state.store import AnnotatorStore
from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import write_table
from tests.postgres import StateDatabase


#: The annotatable source outside rask `work_client` reads, its dataset, and the unit of task t1's item.
WORK_SOURCE = "scans"
WORK_DATASET = f"{WORK_SOURCE}~acme$proj1"
WORK_DOC = "a" * 16
WORK_UNIT = f"/api/annotations/{WORK_DOC}/0/19"
#: The query every call of `work_client` names: project p1, task t1, the dataset.
WORK_QUERY = {"project": "p1", "task": "t1", "dataset": WORK_DATASET}


def work_sources(root: Path) -> list[dict[str, object]]:
    """A directory catalog at ``root`` declared as a source outside rask that git marks annotatable, and nothing else."""
    return [{"kind": "namespace", "name": WORK_SOURCE, "impl": "dir", "properties": {"root": str(root)}, "annotatable": True}]


@pytest.fixture
def state_settings(state_database: StateDatabase, monkeypatch: pytest.MonkeyPatch) -> Iterator[AnnotatorSettings]:
    """The annotator's settings naming this test's database, as the environment a deployment renders."""
    for name, value in state_database.env().items():
        monkeypatch.setenv(name, value)
    get_annotator_settings.cache_clear()
    yield get_annotator_settings()
    get_annotator_settings.cache_clear()


@pytest.fixture
def state_app(state_settings: AnnotatorSettings) -> Callable[..., FastAPI]:
    """Build a test app of the given routers whose lifespan opens the state store and the publish runner."""

    def build(*routers: APIRouter) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
            store = await AnnotatorStore.open(state_settings)
            # No re-drive started: a test's publish runs only when its door starts it.
            runner = PublishRunner(
                store,
                None,
                lease_seconds=state_settings.publish_lease_seconds,
                redrive_seconds=state_settings.publish_redrive_seconds,
                concurrency=state_settings.publish_concurrency,
            )
            app.state.store, app.state.publish_runner = store, runner
            try:
                yield
            finally:
                await runner.close()
                await store.close()

        app = FastAPI(lifespan=lifespan)
        register_handlers(app)
        for router in routers:
            app.include_router(router)
        return app

    return build


@pytest.fixture
def work_client(state_app: Callable[..., FastAPI], tmp_path: Path) -> Iterator[TestClient]:
    """The annotations and task doors over the store, on `WORK_DATASET` with no lakehouse declared; project p1 holds task
    t1, whose item is the unit `WORK_UNIT` names, claimed by the local person (`anon`)."""
    key_fields = ["doc_id", "speech_id", "chunk_id"]
    chunks = pa.table({"doc_id": [WORK_DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())})
    write_table(tmp_path, ["acme", "proj1", "chunks"], chunks, descriptor={"identity": {"key_fields": key_fields}})
    app = state_app(annotations_router, tasks.router)
    app.state.resources = AppState(settings=Settings.model_validate({"WORKBENCH_SOURCES": work_sources(tmp_path)}))
    with TestClient(app) as client:
        store, portal = app.state.store, client.portal
        assert portal is not None, "the client is not running the app"
        portal.call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="p1"))
        task = Task(
            task_id="t1",
            project_id="p1",
            state=TaskState.CLAIMED,
            assignee="anon",
            source=ItemSource(kind="chunks", keys=[f"{WORK_DOC}/0/19"], where=WORK_DATASET),
            media=MediaRef(kind="image"),
        )
        portal.call(store.send_tasks, "p1", [task])
        yield client
