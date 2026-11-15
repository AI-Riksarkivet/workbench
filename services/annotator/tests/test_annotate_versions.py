"""The annotator's version history and time-travel, read off the project's annotations table's own Lance versions.

The project's table (`annotator.annotations.store`) opens through lance-namespace, and a save commits through the
catalog's doors, so the versions a save commits are that table's versions: the ``/versions`` listing walks ``ds.versions()`` and counts the unit at each,
and a ``?version=N`` wire read checks the table out at ``N``. Driven through the real routes on a real lance-namespace
``dir`` catalog with real pylance.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.annotations.router import router as annotations_router
from annotator.annotations.store import table_name
from annotator.api.v1.endpoints import tasks as tasks_ep
from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import namespace_client, write_table


DOC = "a" * 16
DECLARED = {"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}}
SCHEMA = pa.schema([("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64()), ("id", pa.string())])
UNIT = {"doc_id": DOC, "speech_id": 0, "chunk_id": 19}
OTHER = {"doc_id": DOC, "speech_id": 0, "chunk_id": 99}
#: Project p1's annotations on dataset acme$proj1, in the workbench's own namespace.
TABLE = ["workbench", "annotations", table_name("p1", "acme$proj1")]
#: The chunks table's identity columns, which type the unit key's segments.
CHUNKS = pa.table({"doc_id": [DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())})
#: The unit, as task t1 of project p1 names it.
UNIT_PATH = f"/api/annotations/{DOC}/0/19"


class _Task:
    """Task t1's actor: an item of project p1 holding the unit, on the default dataset."""

    async def get(self) -> dict[str, object]:
        return {
            "task_id": "t1",
            "project_id": "p1",
            "state": "claimed",
            "assignee": "anon",
            "source": {"kind": "chunks", "keys": [f"{DOC}/0/19"], "where": None},
            "media": {"kind": "image"},
        }


@pytest.fixture(autouse=True)
def task_t1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: _Task())


def _client(root: Path) -> TestClient:
    app = FastAPI()
    register_handlers(app)
    app.include_router(annotations_router)
    app.state.resources = AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": str(root), "MEDIA_DEFAULT_DATASET": "acme$proj1"}))
    return TestClient(app)


@pytest.fixture
def two_versions(tmp_path: Path) -> TestClient:
    """Project p1's annotations on ``acme$proj1`` at two versions: v1 holds one row of the unit, v2 adds one more and
    another unit's."""
    write_table(tmp_path, ["acme", "proj1", "chunks"], CHUNKS, descriptor=DECLARED)
    write_table(tmp_path, TABLE, pa.Table.from_pylist([{**UNIT, "id": "a1"}], schema=SCHEMA), descriptor=DECLARED)
    ds = lance.dataset(namespace_client=namespace_client(tmp_path), table_id=TABLE)
    ds.merge_insert("id").when_not_matched_insert_all().execute(pa.Table.from_pylist([{**UNIT, "id": "a2"}, {**OTHER, "id": "b1"}], schema=SCHEMA))
    return _client(tmp_path)


def test_the_history_counts_the_unit_at_each_version_newest_first(two_versions: TestClient) -> None:
    response = two_versions.get(f"{UNIT_PATH}/versions?project=p1&task=t1")

    assert response.status_code == 200, response.text
    # The stamp is a commit of its own (v2), so the merge is v3.
    assert [(entry["version"], entry["count"]) for entry in response.json()] == [(3, 2), (2, 1), (1, 1)]


def test_the_history_respects_its_limit(two_versions: TestClient) -> None:
    response = two_versions.get(f"{UNIT_PATH}/versions?project=p1&task=t1&limit=1")

    assert [entry["version"] for entry in response.json()] == [3]


def test_a_dataset_with_no_annotations_table_has_no_history(tmp_path: Path) -> None:
    write_table(tmp_path, ["acme", "proj1", "chunks"], CHUNKS, descriptor=DECLARED)

    response = _client(tmp_path).get(f"{UNIT_PATH}/versions?project=p1&task=t1")

    assert (response.status_code, response.json()) == (200, [])


def test_a_time_travel_read_serves_that_versions_rows(two_versions: TestClient) -> None:
    response = two_versions.get(f"{UNIT_PATH}?project=p1&task=t1&version=1")

    assert response.status_code == 200, response.text
    assert pa.ipc.open_stream(response.content).read_all().column("id").to_pylist() == ["a1"]
    assert response.headers["X-Annotations-Version"] == "1"


def test_a_version_the_table_never_had_is_a_404_problem(two_versions: TestClient) -> None:
    response = two_versions.get(f"{UNIT_PATH}?project=p1&task=t1&version=999")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_the_current_read_serves_the_latest_rows_and_their_version(two_versions: TestClient) -> None:
    response = two_versions.get(f"{UNIT_PATH}?project=p1&task=t1")

    assert response.status_code == 200, response.text
    assert sorted(pa.ipc.open_stream(response.content).read_all().column("id").to_pylist()) == ["a1", "a2"]
    assert response.headers["X-Annotations-Version"] == "3"
