"""A lasso selection's rows (`POST /api/atlas/chunks`) come back as one Arrow IPC stream, in the schema the browser reads.

A selection is up to 1000 full corpus rows, so the viewer answers it columnar: strings ``utf8``, numbers ``float64``
(a browser reads a JSON number as a double), booleans ``bool``, a column with no value a null ``utf8``, and a nested value
(the alignments list) its JSON text named under ``json_columns``. Driven through the real viewer app over a real table on
the ``dir`` catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
from fastapi.testclient import TestClient

from client_kit.media.config import MediaSettings
from tests.media_catalog import write_table
from viewer.main import create_viewer_app


_DESCRIPTOR = {
    "identity": {"key_fields": ["doc_id", "group_id", "chunk_id"]},
    "display": {"title": ["doc_id"], "body": "text", "caption": "note", "metadata": [{"field": "reviewed", "label": "Reviewed"}]},
    "search": {"row_table": "chunks"},
}

_ROWS = pa.table(
    {
        "doc_id": ["d1", "d2"],
        "group_id": pa.array([0, 1], pa.int64()),
        "chunk_id": pa.array([3, 4], pa.int32()),
        "text": ["first", "second"],
        "note": pa.array([None, None], pa.string()),
        "reviewed": [True, False],
    }
)


def test_a_selection_is_one_arrow_stream_in_the_browsers_schema(tmp_path: Path) -> None:
    write_table(tmp_path, ["wh", "corpus", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    app = create_viewer_app(MediaSettings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)}))

    with TestClient(app) as client:
        response = client.post("/api/atlas/chunks", params={"dataset": "wh$corpus"}, json={"rowids": [0, 1]})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    table = pa.ipc.open_stream(response.content).read_all()
    types = {field.name: field.type for field in table.schema}
    assert types == {
        "doc_id": pa.string(),
        "group_id": pa.float64(),
        "chunk_id": pa.float64(),
        "text": pa.string(),
        "note": pa.string(),
        "reviewed": pa.bool_(),
        "alignments": pa.string(),
    }
    assert table.schema.metadata == {b"rows": b"2", b"json_columns": b'["alignments"]'}
    assert sorted(table.to_pylist(), key=lambda row: row["doc_id"]) == [
        {"doc_id": "d1", "group_id": 0.0, "chunk_id": 3.0, "text": "first", "note": None, "reviewed": True, "alignments": json.dumps([])},
        {"doc_id": "d2", "group_id": 1.0, "chunk_id": 4.0, "text": "second", "note": None, "reviewed": False, "alignments": json.dumps([])},
    ]
