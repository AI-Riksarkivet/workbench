"""Listing what the sources hold cannot hang, and one thing that cannot be read is reported, never the whole list's fault.

A source's walk is bounded by pages per listing, names per listing, namespaces, depth and a deadline, and says when it
stopped at one (`truncated`). A child whose name could only be a path or a delimiter is reported rather than asked for,
and a table that raises opening is that dataset's reason while every other source is listed. Driven on real pylance
directory catalogs; only the catalog that answers pages without end is a stand-in, since no real one misbehaves so.
"""

from __future__ import annotations

import time
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import LanceNamespace, ListNamespacesRequest, ListNamespacesResponse, ListTablesRequest, ListTablesResponse

from client_kit.lancekit import registry
from client_kit.media.config import MediaSettings
from client_kit.sources import NamespaceSource
from service_kit.governed.deps import ANONYMOUS_CALLER
from tests.media_catalog import namespace_client, write_table


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

    assert (found.datasets, [(u.id, u.reason) for u in found.unlisted]) == (
        ["odd~fine"],
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


def test_a_table_that_raises_opening_is_its_datasets_reason_and_every_other_source_is_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_table(tmp_path / "first", ["broken"], pa.table({"x": [1]}))
    write_table(tmp_path / "second", ["fine"], pa.table({"x": [1]}))
    settings = MediaSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "first", "impl": "dir", "properties": {"root": str(tmp_path / "first")}},
                {"kind": "namespace", "name": "second", "impl": "dir", "properties": {"root": str(tmp_path / "second")}},
            ]
        }
    )
    real_open = registry._open

    def _raises(client: LanceNamespace, table_id: list[str], session: lance.Session) -> lance.LanceDataset:
        if table_id[-1] == "broken":
            raise OSError("an I/O failure naming s3://a-bucket/a-key")
        return real_open(client, table_id, session)

    monkeypatch.setattr(registry, "_open", _raises)

    surveys = registry.DatasetRegistry(settings).survey(ANONYMOUS_CALLER, {"first", "second"})

    assert [
        (s.discovery.source, s.discovery.datasets, [sorted(d.tables) for d in s.opened], [sorted(d.unreadable.items()) for d in s.opened]) for s in surveys
    ] == [
        ("first", ["first~"], [[]], [[("broken", registry.COULD_NOT_READ)]]),
        ("second", ["second~"], [["fine"]], [[]]),
    ]
