"""The media plane's datasets, found and opened through lance-namespace as the caller.

A dataset is a catalog namespace written ``warehouse$namespace`` (`lance_docs/ns_catalog/namespace/object-relationship.md`
§ Object Identifier), and its tables are what ListTables answers there. Every table opens with
``lance.dataset(namespace_client=..., table_id=[*namespace, table])``, and a lancedb table through a
``LanceNamespaceDBConnection`` on the same namespace client (``open_table(name, namespace_path=namespace)``): both ask
DescribeTable for the table's location and read with the storage options it returns, which on rask carry the credential
the catalog vends this caller (`lance_docs/ns_catalog/spec.yaml` § DescribeTableResponse.storage_options). This process
holds no store credential.

The namespace client presents the caller's own token for the lakehouse, asked per call, so a handle is the caller's: the
cache is keyed on the caller's principal key and the dataset, never on a token, and one caller's handle (and the
credential its tables were opened on) is never served to another. A handle reuses each open until shortly before its
vend expires, so a read costs one DescribeTable (one store session minted on rask) per table per vend lifetime rather than
per request. Refreshing a credential during a long read is
pylance's: a dataset opened through a namespace client builds a storage options provider that asks the namespace again
as the credential nears expiry (pylance 13.0.0, ``lance/__init__.py`` ``dataset`` notes and ``LanceDataset.__init__``).

One ``lance.Session`` and one ``lancedb.Session`` serve every open in the process, so the index and metadata caches are
shared across callers. Measured 2026-11-14 on pylance 13.0.0 and lancedb 0.40.0, against a store that admits one access
key only: a caller opening a table after another, on the same session, signs every request with its own vended key and
is refused, so a session shares no credential between callers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Final

import lance
import lance_namespace
import lancedb
from lance_namespace import DescribeTableRequest, LanceNamespace, ListNamespacesRequest, ListTablesRequest
from lancedb.namespace import LanceNamespaceDBConnection
from pydantic import BaseModel, ConfigDict, PrivateAttr

from client_kit.lancekit.absence import reads_as_absent
from client_kit.lancekit.catalog import NamespaceCatalogWriteTransport, bounded, connect_namespace, translate_catalog_errors
from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY, DatasetDescriptor, Declared, validate_descriptor
from client_kit.lancekit.introspect import TableInfo, dataset_info
from client_kit.media.config import MediaSettings
from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from service_kit.governed.token_exchange import LakehouseCaller
from service_kit.outage import reads_as_store_outage


logger = logging.getLogger(__name__)

#: How many callers' sessions one process keeps (see :class:`CallerSessions`), least recently used dropped first.
CALLER_SESSIONS: Final = 64
#: Each caller's Lance index cache and metadata cache ceilings: a session per caller multiplies what they may hold.
INDEX_CACHE_BYTES: Final = 256 * 1024 * 1024
METADATA_CACHE_BYTES: Final = 64 * 1024 * 1024
#: How many (caller, dataset) handles one process keeps. A handle holds a namespace client and a descriptor, not table
#: bytes, so the bound is on callers times datasets in use, and the least recently used one is dropped first.
HANDLE_CACHE_SIZE: Final = 256
#: How long before a vend's expiry an open stops being reused.
EXPIRY_MARGIN_S: Final = 60.0
#: How long an open on storage options that name no expiry is reused.
UNEXPIRING_OPEN_TTL_S: Final = 300.0


class UnknownDatasetError(LookupError):
    """The id names no dataset the catalog shows this caller."""


class CallerSessions(BaseModel):
    """One caller's Lance and lancedb sessions, which every open that caller makes shares.

    PER CALLER, NEVER PER PROCESS. A session's object-store registry reuses a live store for the same location whatever
    credential the next open was vended: measured 2026-11-14 on pylance 13.0.0 and lancedb 0.40.0, against a store that
    admits one access key, a second caller opening a table on a session where the first caller's open of it was still
    alive read the table signed with the first caller's key, and on a session of its own was refused on its own key.
    So the caches a session holds (indices, metadata, stores) are shared only by one caller's opens.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lance: lance.Session
    lancedb: lancedb.Session

    @classmethod
    def fresh(cls) -> CallerSessions:
        return cls(
            lance=lance.Session(index_cache_size_bytes=INDEX_CACHE_BYTES, metadata_cache_size_bytes=METADATA_CACHE_BYTES),
            lancedb=lancedb.Session(index_cache_size_bytes=INDEX_CACHE_BYTES, metadata_cache_size_bytes=METADATA_CACHE_BYTES),
        )


def _open(client: LanceNamespace, table_id: list[str], session: lance.Session) -> lance.LanceDataset:
    """One table at its latest version, through ``client`` on the storage options its DescribeTable answers."""
    return bounded(lambda: lance.dataset(namespace_client=client, table_id=table_id, session=session))


def reusable_until(storage_options: Mapping[str, str] | None) -> float:
    """The monotonic deadline an open made on ``storage_options`` may be reused until.

    A vend that expires says when, in ``expires_at_millis`` (`lance_docs/ns_catalog/spec.yaml` §
    DescribeTableResponse.storage_options); the open is reused until :data:`EXPIRY_MARGIN_S` before it, so a reader never
    starts on a credential about to lapse. One that names no expiry (a directory catalog's, which vends nothing) is reused
    for :data:`UNEXPIRING_OPEN_TTL_S`, so a grant the catalog withdraws is asked again within that bound.
    """
    expires_at = (storage_options or {}).get("expires_at_millis")
    if expires_at is None:
        return time.monotonic() + UNEXPIRING_OPEN_TTL_S
    return time.monotonic() + (int(expires_at) / 1000 - time.time()) - EXPIRY_MARGIN_S


class _Opened(BaseModel):
    """One table this caller opened, and until when the open may be reused."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: lance.LanceDataset | lancedb.table.Table
    until: float


class DatasetHandle(BaseModel):
    """One dataset as one caller sees it: its namespace, its descriptor, and the caller's namespace client and lancedb
    connection, which every table of it opens through."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    namespace: list[str]
    descriptor: DatasetDescriptor
    client: LanceNamespace
    connection: lancedb.DBConnection
    sessions: CallerSessions

    _sync_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _opened_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    #: This caller's opens of this dataset's tables, by (``lance`` or ``lancedb``, table); a dataset has few tables, so
    #: the handle cache's bound bounds these too.
    _opened: dict[tuple[str, str], _Opened] = PrivateAttr(default_factory=dict)

    def table_id(self, table: str) -> list[str]:
        """The catalog identifier of one of this dataset's tables."""
        return [*self.namespace, table]

    def dataset(self, table: str) -> lance.LanceDataset:
        """One table at its latest version, opened through the namespace on the storage options DescribeTable vends.

        The open is this caller's and is reused until shortly before its vend expires (:func:`reusable_until`): a reuse
        asks the store for the latest version (``latest_version``, a manifest listing on the same vended credential) and
        no DescribeTable, so a read does not mint a store session per request. What a reuse returns is a copy at the
        latest version, never the cached object moved, so concurrent readers do not see each other's checkout.

        Raises:
            NotFoundError: The catalog holds no such table in this dataset.
        """
        ds = self._latest(table)
        self.sync_table_info(table, int(ds.version))
        return ds

    def open_table(self, table: str) -> lancedb.table.Table:
        """One table as a lancedb table, through the same namespace client and on the same vended storage options, reused
        as :meth:`dataset`'s open is; the connection reads the latest version on every read (``read_consistency_interval``
        zero)."""
        key = ("lancedb", table)
        cached = self._reusable(key)
        if isinstance(cached, lancedb.table.Table):
            return cached
        with translate_catalog_errors():
            opened = bounded(lambda: self.connection.open_table(table, namespace_path=self.namespace))
        self._remember(key, opened, opened.to_lance().initial_storage_options)
        return opened

    def writer(self, table: str) -> NamespaceCatalogWriteTransport:
        """The catalog's write doors for one of this dataset's tables, as the same caller: the catalog governs the write."""
        return NamespaceCatalogWriteTransport(self.client, self.table_id(table))

    def has_table(self, table: str) -> bool:
        """Whether the catalog holds ``table`` in this dataset, asked without vending a credential for it."""
        try:
            with translate_catalog_errors():
                bounded(lambda: self.client.describe_table(DescribeTableRequest(id=self.table_id(table), vend_credentials=False)))
        except NotFoundError:
            return False
        return True

    def sync_table_info(self, table: str, observed_version: int) -> None:
        """Fold a version the cached descriptor does not know (a write bumped it, or the table is new) back into it.

        Only that table is re-read, and the tables dict is rebound rather than mutated, so a reader iterating it never
        sees it change underneath. A failed re-read is logged and the cached descriptor kept: the caller already holds a
        good open, and a stale fact must not fail its request.
        """
        info = self.descriptor.tables.get(table)
        if info is not None and info.version == observed_version:
            return
        with self._sync_lock:
            if self.descriptor.tables.get(table) is not info:
                return
            try:
                fresh = dataset_info(self._latest(table), table)
            except Exception:
                logger.warning("table info sync failed for %s/%s", self.id, table, exc_info=True)
                return
            self.descriptor.tables = {**self.descriptor.tables, table: fresh}

    def _latest(self, table: str) -> lance.LanceDataset:
        """``table`` at its latest version, on this caller's reusable open or a new one."""
        key = ("lance", table)
        with translate_catalog_errors():
            try:
                cached = self._reusable(key)
                if isinstance(cached, lance.LanceDataset):
                    return bounded(lambda: cached.checkout_version(cached.latest_version))
                opened = _open(self.client, self.table_id(table), self.sessions.lance)
            except (ValueError, OSError) as exc:
                if reads_as_absent(exc):
                    raise NotFoundError(f"table {table!r} missing from dataset {self.id!r}") from exc
                if reads_as_store_outage(exc):
                    raise ServiceUnavailableError(f"the store behind {table!r} is not answering") from exc
                raise
        self._remember(key, opened, opened.initial_storage_options)
        return opened

    def _reusable(self, key: tuple[str, str]) -> lance.LanceDataset | lancedb.table.Table | None:
        with self._opened_lock:
            entry = self._opened.get(key)
            if entry is None or entry.until <= time.monotonic():
                self._opened.pop(key, None)
                return None
            return entry.table

    def _remember(self, key: tuple[str, str], table: lance.LanceDataset | lancedb.table.Table, storage_options: Mapping[str, str] | None) -> None:
        with self._opened_lock:
            self._opened[key] = _Opened(table=table, until=reusable_until(storage_options))


def table_dataset(handle: DatasetHandle, table: str) -> lance.LanceDataset:
    """One table of ``handle``'s dataset at its latest version; see :meth:`DatasetHandle.dataset`."""
    return handle.dataset(table)


def _pages(list_page: Callable[[str | None], tuple[list[str], str | None]]) -> Iterator[str]:
    """Every name a paginated listing answers, following ``page_token`` until the listing gives none."""
    token: str | None = None
    while True:
        names, token = list_page(token)
        yield from names
        if not token:
            return


def _list_namespaces(client: LanceNamespace, parent: list[str]) -> list[str]:
    def page(token: str | None) -> tuple[list[str], str | None]:
        answer = bounded(lambda: client.list_namespaces(ListNamespacesRequest(id=parent, page_token=token)))
        return sorted(answer.namespaces or []), answer.page_token

    return list(_pages(page))


def _list_tables(client: LanceNamespace, namespace: list[str]) -> list[str]:
    def page(token: str | None) -> tuple[list[str], str | None]:
        answer = bounded(lambda: client.list_tables(ListTablesRequest(id=namespace, page_token=token)))
        return sorted(answer.tables or []), answer.page_token

    return list(_pages(page))


def load_dataset_descriptor(client: LanceNamespace, dataset_id: str, namespace: list[str], descriptor_dir: Path, session: lance.Session) -> DatasetDescriptor:
    """Discover a dataset's tables, read its declared roles, and check the one against the other.

    The declared roles come from ``<descriptor_dir>/<dataset_id>.json`` when that file exists, else from the
    ``lance_media.descriptor`` schema-metadata stamp on the first table (by name) that carries one. A table ListTables
    names but Lance cannot find (dropped in between) is left out; any other read failure propagates, because a flaky read
    is not absence.

    Raises:
        ValueError: The dataset declares no descriptor, or the descriptor names a table or column the dataset lacks.
    """
    tables: dict[str, TableInfo] = {}
    stamped: Declared | None = None
    for name in _list_tables(client, namespace):
        try:
            ds = _open(client, [*namespace, name], session)
        except (ValueError, OSError) as exc:
            if not reads_as_absent(exc):
                raise
            logger.warning("skipping %s: listed but not a readable Lance table: %s", name, exc)
            continue
        tables[name] = dataset_info(ds, name)
        stamp = (ds.schema.metadata or {}).get(DESCRIPTOR_METADATA_KEY)
        if stamped is None and stamp:
            stamped = Declared.model_validate(json.loads(stamp.decode()))
    override = descriptor_dir / f"{dataset_id}.json"
    declared = Declared.model_validate_json(override.read_text()) if override.exists() else stamped
    if declared is None:
        raise ValueError(f"dataset {dataset_id!r}: no declared descriptor (config file or schema stamp)")
    if problems := validate_descriptor(declared, tables):
        raise ValueError(f"dataset {dataset_id!r}: invalid descriptor: " + "; ".join(problems))
    return DatasetDescriptor(id=dataset_id, tables=tables, declared=declared)


class DatasetRegistry:
    """The datasets of one catalog, as each caller sees them, with a bounded cache of the handles opened."""

    def __init__(self, settings: MediaSettings) -> None:
        self._settings = settings
        self._handles: OrderedDict[tuple[str, str], DatasetHandle] = OrderedDict()
        self._sessions: OrderedDict[str, CallerSessions] = OrderedDict()
        self._lock = threading.Lock()

    def list_ids(self, caller: LakehouseCaller) -> list[str]:
        """Every ``warehouse$namespace`` the catalog shows the caller: the root's warehouses, then each one's namespaces.

        A warehouse whose listing fails (refused, or gone since the root listed it) is logged and left out, so one
        warehouse cannot hide the others; a failure to list the root is the caller's answer.
        """
        client = connect_namespace(self._settings, caller.token)
        delimiter = self._settings.catalog_delimiter
        with translate_catalog_errors():
            warehouses = _list_namespaces(client, [])
        ids: list[str] = []
        for warehouse in warehouses:
            try:
                children = _list_namespaces(client, [warehouse])
            except lance_namespace.LanceNamespaceError as exc:
                logger.warning("skipping warehouse %s: its namespaces could not be listed: %s", warehouse, exc)
                continue
            ids.extend(delimiter.join([warehouse, child]) for child in children)
        return ids

    def get(self, dataset_id: str, caller: LakehouseCaller) -> DatasetHandle:
        """The caller's handle on ``dataset_id``, opened on first use and cached for this caller alone.

        Raises:
            UnknownDatasetError: The id is not a warehouse-qualified namespace, or the catalog holds no such namespace.
            ValueError: The dataset's descriptor is missing or invalid.
        """
        key = (caller.subject, dataset_id)
        with self._lock:
            handle = self._handles.get(key)
            if handle is not None:
                self._handles.move_to_end(key)
                return handle
        try:
            namespace = self._settings.catalog_namespace_id(dataset_id)
        except ValueError as exc:
            raise UnknownDatasetError(f"invalid dataset id {dataset_id!r}: {exc}") from exc
        client = connect_namespace(self._settings, caller.token)
        sessions = self._caller_sessions(caller.subject)
        try:
            with translate_catalog_errors():
                descriptor = load_dataset_descriptor(client, dataset_id, namespace, self._settings.descriptor_dir, sessions.lance)
        except NotFoundError as exc:
            raise UnknownDatasetError(f"dataset {dataset_id!r} not found: {exc}") from exc
        # The lancedb connection shares the namespace client, so lancedb's opens carry the same token and trace context.
        connection = LanceNamespaceDBConnection(namespace_client=client, session=sessions.lancedb, read_consistency_interval=timedelta(0))
        handle = DatasetHandle(id=dataset_id, namespace=namespace, descriptor=descriptor, client=client, connection=connection, sessions=sessions)
        with self._lock:
            self._handles[key] = handle
            self._handles.move_to_end(key)
            while len(self._handles) > HANDLE_CACHE_SIZE:
                self._handles.popitem(last=False)
        logger.info("opened dataset %s (%d tables)", dataset_id, len(descriptor.tables))
        return handle

    def _caller_sessions(self, caller: str) -> CallerSessions:
        """``caller``'s sessions, made on first use; the least recently used caller's are dropped past :data:`CALLER_SESSIONS`
        (a handle that still holds them keeps them alive)."""
        with self._lock:
            sessions = self._sessions.get(caller)
            if sessions is None:
                sessions = self._sessions[caller] = CallerSessions.fresh()
            self._sessions.move_to_end(caller)
            while len(self._sessions) > CALLER_SESSIONS:
                self._sessions.popitem(last=False)
            return sessions

    def default(self, caller: LakehouseCaller) -> DatasetHandle:
        """The caller's handle on the configured default dataset.

        Raises:
            UnknownDatasetError: No default dataset is configured.
        """
        if self._settings.default_dataset is None:
            raise UnknownDatasetError("the request names no dataset and MEDIA_DEFAULT_DATASET is not set")
        return self.get(self._settings.default_dataset, caller)
