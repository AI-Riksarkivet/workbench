"""A transient read failure must NOT be laundered into 'does not exist' (SK-05).

Discovering a dataset opens each table ListTables names. Dropping a table on ANY failure made a flaky store read
(connection reset, timeout) make a table that exists vanish, and downstream that absence is indistinguishable from a
missing table: the descriptor cross-check reports ``row_table ... does not exist`` and the whole dataset fails as if it
were misconfigured. A table the catalog lists but whose bytes are gone is real absence: it is named with the reason, and
a descriptor that needs it leaves the dataset shown as a plain table, saying why. A table that cannot be read right now
is named so (with no location or exception text), the dataset is not cached, and the next read sees it again.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import DeclareTableRequest, LanceNamespace

from client_kit.lancekit import registry
from client_kit.media.config import MediaSettings
from service_kit.governed.deps import ANONYMOUS_CALLER
from tests.media_catalog import lakehouse_dir, namespace_client, write_table


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}


def _dataset(root: Path) -> registry.DatasetRegistry:
    write_table(root, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    write_table(root, ["wh", "ns", "documents"], pa.table({"doc_id": ["a"]}), descriptor=_DESCRIPTOR)
    return registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(root))}))


def test_a_table_that_cannot_be_read_right_now_is_named_so_and_the_dataset_read_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    datasets = _dataset(tmp_path)
    real_open = registry._open

    def _reset(client: LanceNamespace, table_id: list[str], session: lance.Session) -> lance.LanceDataset:
        if table_id[-1] == "chunks":
            raise OSError("connection reset by peer at s3://secret-bucket/path")
        return real_open(client, table_id, session)

    monkeypatch.setattr(registry, "_open", _reset)

    first = datasets.get("lake~wh$ns", ANONYMOUS_CALLER)
    monkeypatch.setattr(registry, "_open", real_open)
    again = datasets.get("lake~wh$ns", ANONYMOUS_CALLER)

    assert (first.descriptor.unreadable, first.descriptor.declared, sorted(first.descriptor.tables)) == (
        {"chunks": registry.COULD_NOT_READ},
        None,
        ["documents"],
    )
    assert (again.descriptor.unreadable, again.descriptor.declared is not None) == ({}, True)


def test_a_table_declared_and_never_written_is_named_with_why_and_its_dataset_shown_plain(tmp_path: Path) -> None:
    datasets = _dataset(tmp_path)
    _remove(tmp_path, "chunks")
    namespace_client(tmp_path).declare_table(DeclareTableRequest(id=["wh", "ns", "later"]))

    descriptor = datasets.get("lake~wh$ns", ANONYMOUS_CALLER).descriptor

    assert (descriptor.declared, sorted(descriptor.tables)) == (None, ["documents"])
    assert descriptor.unreadable == {"chunks": registry.LISTED_NOT_WRITTEN, "later": registry.LISTED_NOT_WRITTEN}
    assert descriptor.problem == "its descriptor names what the dataset lacks: search 'default': row_table 'chunks' does not exist"


def _remove(root: Path, table: str) -> None:
    shutil.rmtree(Path(registry.lance.dataset(namespace_client=namespace_client(root), table_id=["wh", "ns", table]).uri.removeprefix("file://")))
