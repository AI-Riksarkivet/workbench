"""Listing what the sources hold cannot hang, and one thing that cannot be read is reported, never the whole list's fault.

A source's walk is bounded by pages per listing, names per listing, namespaces, depth and a deadline, and says when it
stopped at one (`truncated`). A child whose name could only be a path or a delimiter is reported rather than asked for,
and a source that does not answer in time is reported while every other source is listed. A listing opens no table: its
cost is the walk, whatever the datasets hold, and a dataset's facts are read when it is opened. Driven on real pylance
directory catalogs (counted where the work is the claim); only the catalogs that answer pages without end or never
answer are stand-ins, since no real one misbehaves so on demand.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import (
    DescribeTableRequest,
    DescribeTableResponse,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
)

from tests.media_catalog import namespace_client, write_table
from workbench.governed.deps import ANONYMOUS_CALLER
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit import registry
from workbench.media.config import MediaSettings
from workbench.sources import NamespaceSource


class _EndlessEmptyPages(LanceNamespace):
    """A catalog that answers every listing with an empty page and a token for another, forever."""

    def __init__(self) -> None:
        self.calls = 0

    def namespace_id(self) -> str:
        return "endless"

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        self.calls += 1
        return ListNamespacesResponse(namespaces=[], page_token="more")

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        self.calls += 1
        return ListTablesResponse(tables=[], page_token="more")


class _ChildNamedAPath(LanceNamespace):
    """A catalog whose root lists one ordinary child and one whose name holds the id delimiter."""

    def namespace_id(self) -> str:
        return "odd"

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        return ListNamespacesResponse(namespaces=["fine", "a$b"] if not request.id else [])

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        return ListTablesResponse(tables=["t"] if request.id == ["fine"] else [])


def _source(name: str, root: Path) -> NamespaceSource:
    return NamespaceSource.model_validate({"kind": "namespace", "name": name, "impl": "dir", "properties": {"root": str(root)}})


def test_a_catalog_answering_empty_pages_without_end_is_cut_at_the_page_bound(tmp_path: Path) -> None:
    catalog = _EndlessEmptyPages()

    found = registry.discover(_source("endless", tmp_path), catalog, time.monotonic() + 60)

    assert (found.truncated, found.datasets, catalog.calls) == (True, [], 2 * registry.LISTING_PAGES)


def test_a_child_whose_name_holds_the_delimiter_is_reported_not_asked_for(tmp_path: Path) -> None:
    found = registry.discover(_source("odd", tmp_path), _ChildNamedAPath(), time.monotonic() + 60)

    assert ([(d.id, d.tables) for d in found.datasets], [(u.id, u.reason) for u in found.unlisted]) == (
        [("odd~fine", ["t"])],
        [("odd~a$b", "its name holds a delimiter or a path character")],
    )


@pytest.mark.parametrize(
    ("bound", "value"),
    [
        pytest.param("LISTING_NAMES", 1, id="names-per-listing"),
        pytest.param("DISCOVERY_NAMESPACES", 1, id="namespaces"),
        pytest.param("DISCOVERY_DEPTH", 1, id="depth"),
        pytest.param("deadline", 0, id="deadline"),
    ],
)
def test_a_walk_cut_at_a_bound_says_so(bound: str, value: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_table(tmp_path, ["one"], pa.table({"x": [1]}))
    write_table(tmp_path, ["two"], pa.table({"x": [1]}))
    write_table(tmp_path, ["a", "b", "deep"], pa.table({"x": [1]}))
    deadline = time.monotonic() + 60
    if bound == "deadline":
        deadline = time.monotonic() - 1
    else:
        monkeypatch.setattr(registry, bound, value)

    whole = registry.discover(_source("archive", tmp_path), namespace_client(tmp_path), time.monotonic() + 60) if bound == "deadline" else None
    found = registry.discover(_source("archive", tmp_path), namespace_client(tmp_path), deadline)

    assert found.truncated is True
    assert whole is None or whole.truncated is False


class _Counted(LanceNamespace):
    """A source's real directory catalog, every call the registry makes of it counted by operation and id."""

    def __init__(self, inner: LanceNamespace, calls: list[tuple[str, tuple[str, ...]]]) -> None:
        self._inner = inner
        self._calls = calls

    def namespace_id(self) -> str:
        return self._inner.namespace_id()

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        self._calls.append(("list_namespaces", tuple(request.id or [])))
        return self._inner.list_namespaces(request)

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        self._calls.append(("list_tables", tuple(request.id or [])))
        return self._inner.list_tables(request)

    def describe_table(self, request: DescribeTableRequest) -> DescribeTableResponse:
        self._calls.append(("describe_table", tuple(request.id or [])))
        return self._inner.describe_table(request)


class _Hung(LanceNamespace):
    """A catalog that answers no listing until it is released."""

    def __init__(self, released: threading.Event) -> None:
        self._released = released

    def namespace_id(self) -> str:
        return "hung"

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        self._released.wait(30)
        return ListNamespacesResponse(namespaces=[])

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        self._released.wait(30)
        return ListTablesResponse(tables=[])


def _settings(*names: str, root: Path) -> MediaSettings:
    return MediaSettings.model_validate(
        {"WORKBENCH_SOURCES": [{"kind": "namespace", "name": name, "impl": "dir", "properties": {"root": str(root / name)}} for name in names]}
    )


def _admitted(*names: str) -> LakehouseCaller:
    return ANONYMOUS_CALLER.model_copy(update={"admitted": frozenset(names)})


def test_a_listing_opens_no_table_however_many_datasets_a_source_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two dozen namespaces of two tables each: a listing asks each namespace's ListTables and describes no table, and
    a dataset the caller then opens is listed with its tables' facts, the next listing still describing none."""
    namespaces = [f"ns{n:02d}" for n in range(24)]
    for namespace in namespaces:
        for table in ("pages", "words"):
            write_table(tmp_path / "archive", [namespace, table], pa.table({"x": [1, 2]}))
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_connect = registry.connect_as
    monkeypatch.setattr(registry, "connect_as", lambda source, caller: _Counted(real_connect(source, caller), calls))
    datasets = registry.DatasetRegistry(_settings("archive", root=tmp_path))
    caller = _admitted("archive")

    [cold] = datasets.survey(caller)
    listing_calls = sorted({operation for operation, _ in calls})
    calls.clear()
    opened = datasets.get("archive~ns07", caller)
    open_calls = sorted(calls)
    calls.clear()
    [warm] = datasets.survey(caller)

    assert listing_calls == ["list_namespaces", "list_tables"]
    assert [(d.id, d.tables, d.descriptor) for d in cold.datasets] == [(f"archive~{n}", ["pages", "words"], None) for n in namespaces]
    assert [call for call in open_calls if call[0] == "describe_table"] == [("describe_table", ("ns07", "pages")), ("describe_table", ("ns07", "words"))]
    assert [call for call in calls if call[0] == "describe_table"] == []
    assert [(d.id, d.descriptor) for d in warm.datasets if d.descriptor is not None] == [("archive~ns07", opened.descriptor)]
    assert {name: info.row_count for name, info in opened.descriptor.tables.items()} == {"pages": 2, "words": 2}


def test_a_source_that_does_not_answer_in_time_is_reported_and_the_others_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_table(tmp_path / "quick", ["fine"], pa.table({"x": [1]}))
    released = threading.Event()
    real_connect = registry.connect_as
    monkeypatch.setattr(registry, "connect_as", lambda source, caller: _Hung(released) if source.name == "slow" else real_connect(source, caller))
    monkeypatch.setattr(registry, "DISCOVERY_DEADLINE_S", 0.2)
    monkeypatch.setattr(registry, "CALL_TIMEOUT_S", 0.2)

    try:
        surveys = registry.DatasetRegistry(_settings("slow", "quick", root=tmp_path)).survey(_admitted("slow", "quick"))
    finally:
        released.set()

    assert [(s.discovery.source, [d.id for d in s.datasets], [(u.id, u.reason) for u in s.discovery.unlisted]) for s in surveys] == [
        ("slow", [], [("slow~", "it did not answer within 0 s")]),
        ("quick", ["quick~"], []),
    ]
