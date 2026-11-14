"""The annotator's version history and time-travel, read off the annotations table's own Lance versions.

The table opens through lance-namespace as the caller, and a save commits through the catalog's doors, so the versions a
save commits are that table's versions: the ``/versions`` listing walks ``ds.versions()`` and counts the unit at each,
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
from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import namespace_client, write_table


DOC = "a" * 16
DECLARED = {"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}}
SCHEMA = pa.schema([("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64()), ("id", pa.string())])
UNIT = {"doc_id": DOC, "speech_id": 0, "chunk_id": 19}
OTHER = {"doc_id": DOC, "speech_id": 0, "chunk_id": 99}


def _client(root: Path) -> TestClient:
    app = FastAPI()
    register_handlers(app)
    app.include_router(annotations_router)
    app.state.resources = AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": str(root), "MEDIA_DEFAULT_DATASET": "acme$proj1"}))
    return TestClient(app)


@pytest.fixture
def two_versions(tmp_path: Path) -> TestClient:
    """``acme$proj1$annotations`` at two versions: v1 holds one row of the unit, v2 adds one more and another unit's."""
    write_table(tmp_path, ["acme", "proj1", "annotations"], pa.Table.from_pylist([{**UNIT, "id": "a1"}], schema=SCHEMA), descriptor=DECLARED)
    ds = lance.dataset(namespace_client=namespace_client(tmp_path), table_id=["acme", "proj1", "annotations"])
    ds.merge_insert("id").when_not_matched_insert_all().execute(pa.Table.from_pylist([{**UNIT, "id": "a2"}, {**OTHER, "id": "b1"}], schema=SCHEMA))
    return _client(tmp_path)


def test_the_history_counts_the_unit_at_each_version_newest_first(two_versions: TestClient) -> None:
    response = two_versions.get(f"/api/annotations/{DOC}/0/19/versions")

    assert response.status_code == 200, response.text
    # The stamp is a commit of its own (v2), so the merge is v3.
    assert [(entry["version"], entry["count"]) for entry in response.json()] == [(3, 2), (2, 1), (1, 1)]


def test_the_history_respects_its_limit(two_versions: TestClient) -> None:
    response = two_versions.get(f"/api/annotations/{DOC}/0/19/versions?limit=1")

    assert [entry["version"] for entry in response.json()] == [3]


def test_a_dataset_with_no_annotations_table_has_no_history(tmp_path: Path) -> None:
    write_table(tmp_path, ["acme", "proj1", "chunks"], pa.table({"doc_id": [DOC]}), descriptor=DECLARED)

    response = _client(tmp_path).get(f"/api/annotations/{DOC}/0/19/versions")

    assert (response.status_code, response.json()) == (200, [])


def test_a_time_travel_read_serves_that_versions_rows(two_versions: TestClient) -> None:
    response = two_versions.get(f"/api/annotations/{DOC}/0/19?version=1")

    assert response.status_code == 200, response.text
    assert pa.ipc.open_stream(response.content).read_all().column("id").to_pylist() == ["a1"]
    assert response.headers["X-Annotations-Version"] == "1"


def test_a_version_the_table_never_had_is_a_404_problem(two_versions: TestClient) -> None:
    response = two_versions.get(f"/api/annotations/{DOC}/0/19?version=999")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_the_current_read_serves_the_latest_rows_and_their_version(two_versions: TestClient) -> None:
    response = two_versions.get(f"/api/annotations/{DOC}/0/19")

    assert response.status_code == 200, response.text
    assert sorted(pa.ipc.open_stream(response.content).read_all().column("id").to_pylist()) == ["a1", "a2"]
    assert response.headers["X-Annotations-Version"] == "3"
