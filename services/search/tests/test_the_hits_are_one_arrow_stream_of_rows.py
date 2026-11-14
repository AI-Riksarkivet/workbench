"""Search answers its hits (`GET /api/search`) as one Arrow IPC stream of rows, in the schema the browser reads.

The same wire as the viewer's selection door (`service_kit.arrow_rows`): driven through the real search app, a full-text
search over a real indexed table on the ``dir`` catalog.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
from fastapi.testclient import TestClient

from client_kit.media.config import MediaSettings
from client_kit.media.lifespan import make_media_state
from search.main import create_search_app
from tests.media_catalog import write_table


_DESCRIPTOR = {
    "identity": {"key_fields": ["doc_id"]},
    "display": {"body": "text"},
    "search": {"row_table": "chunks", "fts": {"table": "chunks", "column": "text"}},
}


def test_the_hits_are_one_arrow_stream_of_rows(tmp_path: Path) -> None:
    ds = write_table(tmp_path, ["wh", "corpus", "chunks"], pa.table({"doc_id": ["d1", "d2"], "text": ["a ship sails", "a cart rolls"]}), descriptor=_DESCRIPTOR)
    ds.create_scalar_index("text", index_type="INVERTED")
    settings = MediaSettings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)})

    with TestClient(create_search_app(make_media_state(settings), settings)) as client:
        response = client.get("/api/search", params={"q": "ship", "mode": "fts", "dataset": "wh$corpus"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.schema.metadata[b"rows"] == b"1"
    hit = table.to_pylist()[0]
    assert (hit["doc_id"], hit["text"]) == ("d1", "a ship sails")
    assert table.schema.field("_score").type == pa.float64()
