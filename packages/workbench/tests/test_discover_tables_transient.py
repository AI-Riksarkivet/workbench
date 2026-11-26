"""A transient read failure must NOT be laundered into 'does not exist'.

Discovering a dataset opens each table ListTables names. Dropping a table on ANY failure made a flaky store read
(connection reset, timeout) make a table that exists vanish, and downstream that absence is indistinguishable from a
missing table: the descriptor cross-check reports ``row_table ... does not exist`` and the whole dataset fails as if it
were misconfigured. A table the catalog lists but whose bytes are gone is real absence: it is named with the reason, and
a descriptor that needs it leaves the dataset shown as a plain table, saying why. A table that cannot be read right now
is named so (with no location or exception text), the dataset is not cached, and the next read sees it again.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import DeclareTableRequest, DescribeTableRequest, DescribeTableResponse, LanceNamespace, PermissionDeniedError

from tests.media_catalog import lakehouse_dir, namespace_client, write_table
from workbench.governed.deps import ANONYMOUS_CALLER
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit import registry
from workbench.media.config import MediaSettings


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


#: How soon a table written since is seen, as the registry promises it (`ABSENT_RECHECK_S`).
A_MINUTE = 60.0


class _Clock:
    """A monotonic clock a test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _gone_then_back(root: Path, table: str, namespace: str = "ns") -> Callable[[], object]:
    """Move ``table``'s versions aside, as when its data is removed; the callable puts them back, as a write since does."""
    uri = lance.dataset(namespace_client=namespace_client(root), table_id=["wh", namespace, table]).uri.removeprefix("file://")
    versions, aside = Path(uri) / "_versions", root / f"{namespace}-{table}-versions-aside"
    shutil.move(versions, aside)
    return lambda: shutil.move(aside, versions)


def _opened_tables(datasets: registry.DatasetRegistry, caller: LakehouseCaller, namespace: str = "ns") -> list[str] | str:
    try:
        return sorted(datasets.get(f"lake~wh${namespace}", caller).descriptor.tables)
    except registry.DatasetAbsentError:
        return "absent"


def test_a_dataset_whose_gone_table_is_still_gone_costs_a_listing_and_that_tables_open_when_rechecked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recheck asks only what can have changed: the dataset's names (one ListTables) and the gone table (one open,
    one DescribeTable). Its other tables are not described again: measured on the dev cluster, a dataset of 17 readable
    tables beside 2 gone ones took about 6 s every minute when the recheck opened it whole."""
    clock = _Clock()
    for table in ("chunks", "a", "b", "c", "d"):
        write_table(tmp_path, ["wh", "ns", table], pa.table({"doc_id": ["a"]}))
    datasets = registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), clock=clock)
    _gone_then_back(tmp_path, "chunks")
    first = _opened_tables(datasets, ANONYMOUS_CALLER)
    calls: list[str] = []
    client_type = type(namespace_client(tmp_path))
    for name in ("describe_table", "list_tables", "list_namespaces"):
        method = getattr(client_type, name)
        monkeypatch.setattr(client_type, name, lambda *args, _name=name, _method=method, **kwargs: calls.append(_name) or _method(*args, **kwargs))

    clock.now += A_MINUTE
    rechecked = _opened_tables(datasets, ANONYMOUS_CALLER)

    assert (first, rechecked, sorted(calls)) == (["a", "b", "c", "d"], ["a", "b", "c", "d"], ["describe_table", "list_tables"])


def test_the_absences_remembered_are_bounded_and_the_oldest_is_forgotten_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "HANDLES_PER_SOURCE", 1)
    for namespace in ("first", "second"):
        write_table(tmp_path, ["wh", namespace, "chunks"], pa.table({"doc_id": ["a"]}))
    datasets = registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), clock=_Clock())
    back = [_gone_then_back(tmp_path, "chunks", namespace) for namespace in ("first", "second")]

    found = [_opened_tables(datasets, ANONYMOUS_CALLER, namespace) for namespace in ("first", "second")]
    for put_back in back:
        put_back()
    again = [_opened_tables(datasets, ANONYMOUS_CALLER, namespace) for namespace in ("first", "second")]

    assert (found, again) == (["absent", "absent"], [["chunks"], "absent"])


def test_a_dataset_whose_tables_are_all_gone_is_opened_again_once_the_interval_has_passed(tmp_path: Path) -> None:
    clock = _Clock()
    write_table(tmp_path, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    datasets = registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), clock=clock)
    back = _gone_then_back(tmp_path, "chunks")

    first = _opened_tables(datasets, ANONYMOUS_CALLER)
    back()
    within = _opened_tables(datasets, ANONYMOUS_CALLER)
    clock.now += A_MINUTE
    after = _opened_tables(datasets, ANONYMOUS_CALLER)

    assert (first, within, after) == ("absent", "absent", ["chunks"])


def test_one_callers_absent_answer_is_never_another_callers(tmp_path: Path) -> None:
    write_table(tmp_path, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    datasets = registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), clock=_Clock())
    back = _gone_then_back(tmp_path, "chunks")

    first = _opened_tables(datasets, ANONYMOUS_CALLER)
    back()
    other = _opened_tables(datasets, LakehouseCaller(subject="dex~someone-else"))

    assert (first, other) == ("absent", ["chunks"])


def test_a_cached_dataset_holding_a_gone_table_is_opened_again_once_the_interval_has_passed(tmp_path: Path) -> None:
    clock = _Clock()
    write_table(tmp_path, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    write_table(tmp_path, ["wh", "ns", "documents"], pa.table({"doc_id": ["a"]}))
    datasets = registry.DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), clock=clock)
    back = _gone_then_back(tmp_path, "chunks")

    first = _opened_tables(datasets, ANONYMOUS_CALLER)
    back()
    within = _opened_tables(datasets, ANONYMOUS_CALLER)
    clock.now += A_MINUTE
    after = _opened_tables(datasets, ANONYMOUS_CALLER)

    assert (first, within, after) == (["documents"], ["documents"], ["chunks", "documents"])


def _remove(root: Path, table: str) -> None:
    shutil.rmtree(Path(registry.lance.dataset(namespace_client=namespace_client(root), table_id=["wh", "ns", table]).uri.removeprefix("file://")))


class _RefusingCatalog(lance.namespace.DirectoryNamespace):
    """A catalog that lists every table and refuses to describe the ones named in ``refused``, as a lakehouse answers a
    caller who may describe a table but not read its rows (403 code 15, rask's `describer` level). Each describe asked is
    counted by table."""

    def __init__(self, root: Path) -> None:
        super().__init__(root=str(root))
        self.refused: set[str] = set()
        self.described: list[str] = []

    def describe_table(self, request: DescribeTableRequest) -> DescribeTableResponse:
        table = (request.id or [""])[-1]
        self.described.append(table)
        if table in self.refused:
            raise PermissionDeniedError(f"the caller may describe table {table} but not read its data")
        return super().describe_table(request)


class _OnCatalog(registry.DatasetRegistry):
    """The registry, reading every source through one catalog the test holds."""

    def __init__(self, settings: MediaSettings, catalog: LanceNamespace, clock: _Clock) -> None:
        super().__init__(settings, clock=clock)
        self._catalog = catalog

    def client(self, source: object, caller: LakehouseCaller) -> LanceNamespace:
        return self._catalog


def test_a_table_the_catalog_lists_but_refuses_is_named_with_why_the_rest_opens_and_a_later_grant_is_seen(tmp_path: Path) -> None:
    """A refused table is the table's answer, not the dataset's: the caller's other tables open, the descriptor that
    needs it says it is refused rather than missing, the cached dataset asks only that table again while it stays
    refused, and it opens once the caller is granted it."""
    clock = _Clock()
    write_table(tmp_path, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}))
    write_table(tmp_path, ["wh", "ns", "documents"], pa.table({"doc_id": ["a"]}), descriptor=_DESCRIPTOR)
    catalog = _RefusingCatalog(tmp_path)
    catalog.refused = {"chunks"}
    datasets = _OnCatalog(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}), catalog, clock)

    refused = datasets.get("lake~wh$ns", ANONYMOUS_CALLER).descriptor
    clock.now += A_MINUTE
    catalog.described.clear()
    still = _opened_tables(datasets, ANONYMOUS_CALLER)
    asked_again = list(catalog.described)
    catalog.refused.clear()
    clock.now += A_MINUTE
    granted = datasets.get("lake~wh$ns", ANONYMOUS_CALLER).descriptor

    assert (sorted(refused.tables), refused.unreadable, refused.declared) == (["documents"], {"chunks": registry.LISTED_NOT_YOURS}, None)
    assert refused.problem == f"its descriptor names chunks, which {registry.LISTED_NOT_YOURS}"
    assert (still, asked_again) == (["documents"], ["chunks"])
    assert (sorted(granted.tables), granted.unreadable, granted.declared is not None) == (["chunks", "documents"], {}, True)
