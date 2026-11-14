"""A canvas name is read the way the catalog holds it: a table is its own input, a dataset names its row table.

A name's first segment is its warehouse (`docs/rask/COMMIT-EVENTS.md`), so ``acme$media`` is a dataset (a namespace)
and ``acme$media$chunks`` may be a table or a nested namespace. The catalog decides which, never the segment count alone.
Driven on a real lance-namespace ``dir`` catalog with real pylance.
"""

from __future__ import annotations

import json
from pathlib import Path

import lance_namespace
import lancedb
import pyarrow as pa
import pytest

from annotator import corpus
from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY
from client_kit.media.config import Settings
from client_kit.media.state import AppState


DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "searches": [{"name": "default", "row_table": "chunks"}]}


@pytest.fixture
def catalog(tmp_path: Path) -> AppState:
    root = str(tmp_path)
    client = lance_namespace.connect("dir", {"root": root})
    for namespace in (["acme"], ["acme", "media"], ["acme", "media", "nested"]):
        client.create_namespace(lance_namespace.CreateNamespaceRequest(id=namespace))
    rows = pa.table(
        {"doc_id": ["d1"], "text": ["hello"]},
        schema=pa.schema([("doc_id", pa.string()), ("text", pa.string())], metadata={DESCRIPTOR_METADATA_KEY: json.dumps(DESCRIPTOR).encode()}),
    )
    lancedb.connect_namespace("dir", {"root": root}).create_table("chunks", rows, namespace_path=["acme", "media"])
    return AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": root}))


def test_input_tables_reads_each_name_as_the_catalog_holds_it(catalog: AppState) -> None:
    names = ["acme$media$chunks", "acme$media", "acme$media$nested", "acme"]

    found = corpus.input_tables(catalog, names, caller_token=None)

    assert found == {"acme$media$chunks": "acme$media$chunks", "acme$media": "acme$media$chunks"}
