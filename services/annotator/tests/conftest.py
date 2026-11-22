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

import json
import threading
import time
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

import pyarrow as pa
import pytest
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from lance import blob_array, blob_field
from starlette.types import Message, Receive, Scope, Send

from annotator.annotations.router import router as annotations_router
from annotator.api.v1.endpoints import assist, project_events, projects, tasks
from annotator.api.v1.endpoints.judge import router as judge_router
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.model_endpoint.stand_in import create_stand_in
from annotator.projects.lakehouse import PublishRunner
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, Task, TaskState
from annotator.projects.ontology import LabelClass, LabelOntology
from annotator.state.store import AnnotatorStore
from tests.media_catalog import write_table
from tests.model_item import DATASET, DOC, PICTURE, SOURCE, UNIT, Build, StandIn
from tests.postgres import StateDatabase
from workbench.exceptions import register_handlers
from workbench.media.state import AppState


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
        app.state.resources = AppState(settings=state_settings)
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
    app.state.resources = AppState(settings=AnnotatorSettings.model_validate({"WORKBENCH_SOURCES": work_sources(tmp_path)}))
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


@pytest.fixture(scope="module")
def running_stand_in() -> Iterator[StandIn]:
    """The stand-in model endpoint on a socket of its own, recording what it is asked."""
    app = create_stand_in()
    # Its address is known once it listens; the list is the one its recorder fills.
    rig = StandIn(url="http://127.0.0.1", asks=[])

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        """The stand-in, with each request it reads kept; its answer passes through as it is sent, byte by byte."""
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        body = bytearray()

        async def read() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    headers = dict(scope["headers"])
                    rig.asks.append(
                        {"path": scope["path"], "authorization": headers.get(b"authorization", b"").decode(), "body": json.loads(body) if body else None}
                    )
            return message

        await app(scope, read, send)

    server = uvicorn.Server(uvicorn.Config(recording, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the stand-in endpoint did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    rig.url = f"http://127.0.0.1:{port}"
    yield rig
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def endpoint(running_stand_in: StandIn) -> str:
    """The stand-in's address, with nothing asked of it yet."""
    running_stand_in.asks.clear()
    return running_stand_in.url


@pytest.fixture
def asks(running_stand_in: StandIn, endpoint: str) -> list[dict[str, object]]:
    """Every call the stand-in was sent by the test that is running."""
    return running_stand_in.asks


@pytest.fixture
def client_for(state_app: Callable[..., FastAPI], tmp_path: Path) -> Iterator[Build]:
    """The assist, annotations and task doors over the store, on a dataset whose unit has a frame; project p1 holds task
    t1 (the unit, claimed by the local person, labelling with the classes `seal` and `stamp`). ``endpoint`` names the
    model endpoint, or none; ``presents`` is the token the annotator presents to it, ``picture`` the unit's frame, and
    ``judge`` the model the project names as its judge; ``tools_of`` gives a class tools of its own."""
    key_fields = ["doc_id", "speech_id", "chunk_id"]
    descriptor = {"identity": {"key_fields": key_fields}, "capabilities": {"frames": "frames.frame"}}
    keys = {"doc_id": [DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())}
    write_table(tmp_path, ["acme", "proj1", "chunks"], pa.table(keys), descriptor=descriptor)
    schema = pa.schema(
        [
            ("doc_id", pa.string()),
            ("speech_id", pa.int64()),
            ("chunk_id", pa.int64()),
            ("frame_idx", pa.int64()),
            ("frame_mime", pa.string()),
            blob_field("frame"),
        ]
    )
    token = tmp_path / "model-endpoint-token"
    clients = ExitStack()

    def build(
        endpoint: str | None,
        *,
        timeout: float = 30.0,
        tools: Sequence[str] = ("bbox", "polygon"),
        presents: str = "the-annotators-own-token",
        picture: bytes = PICTURE,
        judge: str = "",
        tools_of: dict[str, Sequence[str]] | None = None,
    ) -> TestClient:
        frames = pa.table(
            {**keys, "frame_idx": pa.array([0], pa.int64()), "frame_mime": ["image/png; charset=binary"], "frame": blob_array([picture])},
            schema=schema,
        )
        write_table(tmp_path, ["acme", "proj1", "frames"], frames)
        token.write_text(presents + "\n")
        values: dict[str, object] = {
            "WORKBENCH_SOURCES": [{"kind": "namespace", "name": SOURCE, "impl": "dir", "properties": {"root": str(tmp_path)}, "annotatable": True}],
            "ANNOTATOR_MODEL_ENDPOINT_TIMEOUT_SECONDS": timeout,
        }
        if endpoint is not None:
            values |= {"ANNOTATOR_MODEL_ENDPOINT_URL": endpoint, "ANNOTATOR_MODEL_ENDPOINT_TOKEN_FILE": str(token)}
        app = state_app(assist.router, judge_router, annotations_router, tasks.router, projects.router, project_events.router)
        app.state.resources = AppState(settings=AnnotatorSettings.model_validate(values))
        client = clients.enter_context(TestClient(app))
        store, portal = app.state.store, client.portal
        assert portal is not None, "the client is not running the app"
        ontology = LabelOntology(
            classes=[LabelClass.model_validate({"name": name, "tools": list((tools_of or {}).get(name, tools))}) for name in ("seal", "stamp")]
        )
        portal.call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="p1", ontology=ontology, judge_model=judge))
        task = Task(
            task_id="t1",
            project_id="p1",
            state=TaskState.CLAIMED,
            assignee="anon",
            source=ItemSource(kind="chunks", keys=[UNIT], where=DATASET),
            media=MediaRef(kind="image"),
            ontology=ontology,
        )
        portal.call(store.send_tasks, "p1", [task])
        return client

    with clients:
        yield build
