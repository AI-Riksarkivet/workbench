"""A transient read failure must NOT be laundered into 'does not exist' (SK-05).

Discovering a dataset opens each table ListTables names. Dropping a table on ANY failure made a flaky store read
(connection reset, timeout) make a table that exists vanish, and downstream that absence is indistinguishable from a
missing table: the descriptor cross-check reports ``row_table ... does not exist`` and the whole dataset fails as if it
were misconfigured. A table the catalog lists but whose bytes are gone is real absence and is still dropped, so the
cross-check names it; an I/O error must surface, loud and retriable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow as pa
import pytest

from client_kit.lancekit import registry
from client_kit.media.config import MediaSettings
from service_kit.governed.deps import ANONYMOUS_CALLER
from tests.media_catalog import namespace_client, write_table


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}


def _dataset(root: Path) -> registry.DatasetRegistry:
    write_table(root, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    write_table(root, ["wh", "ns", "documents"], pa.table({"doc_id": ["a"]}), descriptor=_DESCRIPTOR)
    return registry.DatasetRegistry(MediaSettings.model_validate({"MEDIA_CATALOG_DIR": str(root)}))


def test_transient_read_error_propagates_not_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    datasets = _dataset(tmp_path)

    def _reset(_client: object, _table_id: list[str], _session: object) -> None:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(registry, "_open", _reset)

    with pytest.raises(OSError, match="connection reset"):
        datasets.get("wh$ns", ANONYMOUS_CALLER)


def test_a_listed_table_whose_bytes_are_gone_is_dropped_and_named(tmp_path: Path) -> None:
    datasets = _dataset(tmp_path)
    shutil.rmtree(Path(registry.lance.dataset(namespace_client=namespace_client(tmp_path), table_id=["wh", "ns", "chunks"]).uri.removeprefix("file://")))

    with pytest.raises(ValueError, match="row_table 'chunks' does not exist"):
        datasets.get("wh$ns", ANONYMOUS_CALLER)
