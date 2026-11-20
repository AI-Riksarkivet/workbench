"""The media plane's datasets, found and opened through lance-namespace as the caller, in every declared source.

A dataset is a namespace of a source that holds tables, at any depth, written ``<source>~<namespace id>``
(`client_kit.sources`; `lance_docs/ns_catalog/namespace/object-relationship.md` § Object Identifier), and its tables are
what ListTables answers there. Every table opens with
``lance.dataset(namespace_client=..., table_id=[*namespace, table])``, and a lancedb table through a
``LanceNamespaceDBConnection`` on the same namespace client (``open_table(name, namespace_path=namespace)``): both ask
DescribeTable for the table's location and read with the storage options it returns, which on rask carry the credential
the catalog vends this caller (`lance_docs/ns_catalog/spec.yaml` § DescribeTableResponse.storage_options). This process
holds no store credential: a source that vends none is read on the workload's own identity.

On a lakehouse the namespace client presents the caller's own token, asked per call, so a handle is the caller's: the
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

import contextvars
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta
from pathlib import Path
from typing import Final

import lance
import lance_namespace
import lancedb
from lance_namespace import DescribeTableRequest, LanceNamespace, ListNamespacesRequest, ListTablesRequest
from lancedb.namespace import LanceNamespaceDBConnection
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from client_kit.lancekit.absence import reads_as_absent
from client_kit.lancekit.catalog import CALL_TIMEOUT_S, bounded, translate_catalog_errors
from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY, DatasetDescriptor, Declared, validate_descriptor
from client_kit.lancekit.introspect import TableInfo, dataset_info
from client_kit.media.config import MediaSettings
from client_kit.source_names import check_segments
from client_kit.sources import NamespaceSource, RaskSource, TablesSource, annotatable, connect_as, qualify, require_admitted
from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError
from service_kit.governed.token_exchange import LakehouseCaller
from service_kit.outage import reads_as_store_outage


logger = logging.getLogger(__name__)

#: How many callers' sessions one process keeps (see :class:`CallerSessions`), least recently used dropped first.
CALLER_SESSIONS: Final = 64
#: Each caller's Lance index cache and metadata cache ceilings: a session per caller multiplies what they may hold.
INDEX_CACHE_BYTES: Final = 256 * 1024 * 1024
METADATA_CACHE_BYTES: Final = 64 * 1024 * 1024
#: How many (caller, dataset) handles one process keeps per source. A handle holds a namespace client and a descriptor,
#: not table bytes. One listing opens up to :data:`DISCOVERY_NAMESPACES` datasets of each source, so the cache holds
#: several listings' worth: a listing never evicts the handles it opened itself.
HANDLES_PER_SOURCE: Final = 4 * 256
#: How long before a vend's expiry an open stops being reused.
EXPIRY_MARGIN_S: Final = 60.0
#: How long an open on storage options that name no expiry is reused.
UNEXPIRING_OPEN_TTL_S: Final = 300.0
#: The most namespaces one source's discovery visits, and the deepest it walks: a catalog's shape is its own, and a
#: listing a person waits on is bounded whatever that shape is.
DISCOVERY_NAMESPACES: Final = 256
DISCOVERY_DEPTH: Final = 8
#: The most names one listing's pages may answer, and the most pages it reads, past which the listing is cut and says so.
LISTING_NAMES: Final = 2000
LISTING_PAGES: Final = 100
#: How long one source's discovery may take, every listing and open of it together, before it is cut and says so.
DISCOVERY_DEADLINE_S: Final = 20.0
#: What a table the catalog lists and Lance cannot find is, in words a person reads. DeclareTable reserves a name and a
#: location and writes no data (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md` § DeclareTable), so a
#: declared table no one has written is listed with no version to open, as one whose data was removed is; measured
#: 2026-11-16, the dev lakehouse lists three such tables, each with a `declare_table` its only event and no version.
LISTED_NOT_WRITTEN: Final = "the catalog lists it, but holds no version of it to read: declared and never written, or its data removed"
#: How long a dataset found holding a listed table with no version to read is answered as found, before it is opened
#: again: a listing every page makes does not open a table that is not there each time, and a table written since is
#: seen within this.
ABSENT_RECHECK_S: Final = 60.0


class UnknownDatasetError(LookupError):
    """The id names no dataset the catalog shows this caller."""


class DatasetAbsentError(UnknownDatasetError):
    """Every table the catalog lists in the dataset holds no version to read (:data:`LISTED_NOT_WRITTEN`)."""


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
    source: RaskSource | NamespaceSource | TablesSource
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

    @property
    def declared(self) -> Declared:
        """The dataset's declared roles.

        Raises:
            NotFoundError: The dataset declares none: it is shown as a plain table, and a door that needs a role refuses.
        """
        declared = self.descriptor.declared
        if declared is None:
            why = f" ({self.descriptor.problem})" if self.descriptor.problem else ""
            raise NotFoundError(f"dataset {self.id!r} declares no workbench descriptor it can use{why}: it is shown as a plain table")
        return declared

    def qualified_table(self, table: str) -> str:
        """A table of this dataset by its qualified name (``<source>~<namespace>$<table>``)."""
        return qualify(self.source, self.table_id(table))

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


class Listed(BaseModel):
    """What one listing answered, sorted, and whether it was cut at :data:`LISTING_NAMES` or :data:`LISTING_PAGES`."""

    names: list[str]
    truncated: bool = False


def _bounded_names(list_page: Callable[[str | None], tuple[list[str], str | None]]) -> Listed:
    """Every name a paginated listing answers, following ``page_token`` (`lance_docs/ns_catalog/spec.yaml` §
    PageToken) for at most :data:`LISTING_PAGES` pages and :data:`LISTING_NAMES` names: a catalog that answers pages
    without end, empty or not, is cut there."""
    names: list[str] = []
    token: str | None = None
    for _ in range(LISTING_PAGES):
        page, token = list_page(token)
        for name in page:
            if len(names) == LISTING_NAMES:
                return Listed(names=sorted(names), truncated=True)
            names.append(name)
        if not token:
            return Listed(names=sorted(names))
    return Listed(names=sorted(names), truncated=True)


def _list_namespaces(client: LanceNamespace, parent: list[str]) -> Listed:
    def page(token: str | None) -> tuple[list[str], str | None]:
        answer = bounded(lambda: client.list_namespaces(ListNamespacesRequest(id=parent, page_token=token)))
        return list(answer.namespaces or []), answer.page_token

    return _bounded_names(page)


def _list_tables(client: LanceNamespace, namespace: list[str]) -> Listed:
    def page(token: str | None) -> tuple[list[str], str | None]:
        answer = bounded(lambda: client.list_tables(ListTablesRequest(id=namespace, page_token=token)))
        return list(answer.tables or []), answer.page_token

    return _bounded_names(page)


class Unlisted(BaseModel):
    """A namespace, or a whole source, whose listing could not be read, and why."""

    id: str
    reason: str


class SourceDiscovery(BaseModel):
    """One source as the caller sees it: the datasets (namespaces holding tables) found, what could not be listed, and
    whether the walk stopped at its bounds before seeing everything."""

    source: str
    kind: str
    title: str
    datasets: list[str] = Field(default_factory=list)
    unlisted: list[Unlisted] = Field(default_factory=list)
    truncated: bool = False


#: A failure as a person reading the dataset list is told it: a short stable reason, with no location, path or exception
#: text, which go to the log beside the request's trace.
_REASONS: Final = (
    (ForbiddenError, "the catalog does not let you read it"),
    (lance_namespace.PermissionDeniedError, "the catalog does not let you read it"),
    (UnauthorizedError, "the catalog did not accept the credential"),
    (lance_namespace.UnauthenticatedError, "the catalog did not accept the credential"),
    (NotFoundError, "it is not there any more"),
    (lance_namespace.TableNotFoundError, "it is not there any more"),
    (lance_namespace.NamespaceNotFoundError, "it is not there any more"),
    (ServiceUnavailableError, "the catalog or its store did not answer"),
    (DatasetAbsentError, LISTED_NOT_WRITTEN),
)
COULD_NOT_READ: Final = "it could not be read right now"
_REFUSALS: Final = (ForbiddenError, UnauthorizedError, lance_namespace.PermissionDeniedError, lance_namespace.UnauthenticatedError)


def reason_for(exc: BaseException) -> str:
    """The reason a person reads for ``exc``."""
    return next((reason for kind, reason in _REASONS if isinstance(exc, kind)), COULD_NOT_READ)


def _tables_of(source: RaskSource | NamespaceSource | TablesSource, listed: list[str]) -> list[str]:
    if isinstance(source, TablesSource) and source.single is not None:
        return [name for name in listed if name == source.single]
    return listed


def discover(
    source: RaskSource | NamespaceSource | TablesSource, client: LanceNamespace, deadline: float, *, clock: Callable[[], float] = time.monotonic
) -> SourceDiscovery:
    """Every namespace of ``source`` that holds tables, found by walking ListNamespaces from the root and asking each
    namespace's ListTables: tables may sit at the root, one level down, or at any depth
    (`lance_docs/ns_catalog/namespace/index.md` § Namespace Hierarchy; a V1 directory catalog lists no child namespace
    and every table at the root, `lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md` § ListNamespaces).

    Bounded by :data:`DISCOVERY_NAMESPACES`, :data:`DISCOVERY_DEPTH`, :data:`LISTING_NAMES`, :data:`LISTING_PAGES` and
    ``deadline`` (a time on ``clock``), and honest: a namespace whose listing fails, and a child whose name could not be one
    (it holds a delimiter or a path character), is reported with its reason, and a walk cut at a bound says so. A
    lakehouse's warehouses (the first level) serve no tables, so only deeper namespaces are asked for theirs.
    """
    found = SourceDiscovery(source=source.name, kind=source.kind, title=source.title)
    queue: list[list[str]] = [[]]
    visited = 0
    while queue:
        namespace = queue.pop(0)
        visited += 1
        if visited > DISCOVERY_NAMESPACES or clock() > deadline:
            found.truncated = True
            break
        label = qualify(source, namespace)
        lakehouse_container = isinstance(source, RaskSource) and len(namespace) < 2
        try:
            with translate_catalog_errors():
                if not lakehouse_container:
                    tables = _list_tables(client, namespace)
                    found.truncated |= tables.truncated
                    if _tables_of(source, tables.names):
                        found.datasets.append(label)
                children = _list_namespaces(client, namespace) if len(namespace) < DISCOVERY_DEPTH else Listed(names=[], truncated=True)
        except Exception as exc:
            logger.warning("namespace %s could not be listed", label, exc_info=True)
            found.unlisted.append(Unlisted(id=label, reason=reason_for(exc)))
            if not namespace:
                # The root itself: the whole source could not be read, which the caller is told as such.
                return found
            continue
        found.truncated |= children.truncated
        for child in children.names:
            try:
                queue.append(check_segments([*namespace, child], source.delimiter))
            except ValueError:
                found.unlisted.append(
                    Unlisted(id=f"{label}{source.delimiter if namespace else ''}{child}", reason="its name holds a delimiter or a path character")
                )
    return found


def load_dataset_descriptor(
    client: LanceNamespace,
    source: RaskSource | NamespaceSource | TablesSource,
    dataset_id: str,
    namespace: list[str],
    descriptor_dir: Path,
    session: lance.Session,
) -> DatasetDescriptor:
    """Discover a dataset's tables, read its declared roles, and check the one against the other.

    The declared roles come from ``<descriptor_dir>/<dataset_id>.json`` when that file exists, else from the
    ``lance_media.descriptor`` schema-metadata stamp on the first table (by name) that carries one. A dataset with
    neither, or whose roles name a table or column it lacks, is served with no declared roles (a plain table, the reason
    kept in ``problem``): nothing is guessed. A table ListTables names that cannot be opened is kept out of ``tables`` and
    named in ``unreadable`` with why: absent, refused, or (any other failure) unreadable right now, which is not absence,
    so the dataset is then not cached (``transient``) and its declared roles are not judged against the tables it has.
    """
    tables: dict[str, TableInfo] = {}
    unreadable: dict[str, str] = {}
    transient = False
    stamped: Declared | None = None
    problem: str | None = None
    listed = _list_tables(client, namespace)
    for name in _tables_of(source, listed.names):
        try:
            check_segments([name], source.delimiter)
            ds = _open(client, [*namespace, name], session)
            info = dataset_info(ds, name)
        except _REFUSALS:
            # A refusal is the caller's answer for the dataset, never a table's reason to show it without that table.
            raise
        except Exception as exc:
            absent = isinstance(exc, ValueError | OSError) and reads_as_absent(exc)
            if absent:
                # An expected condition (a table declared and never written, or its data removed): one line, which names
                # the table and not where its data was looked for.
                logger.warning("table %s of %s is listed but holds no version to read", name, dataset_id)
            else:
                logger.warning("table %s of %s could not be opened", name, dataset_id, exc_info=True)
            unreadable[name] = LISTED_NOT_WRITTEN if absent else reason_for(exc)
            transient |= unreadable[name] == COULD_NOT_READ or isinstance(exc, ServiceUnavailableError)
            continue
        tables[name] = info
        stamp = (ds.schema.metadata or {}).get(DESCRIPTOR_METADATA_KEY)
        if stamped is None and problem is None and stamp:
            try:
                stamped = Declared.model_validate(json.loads(stamp.decode()))
            except (ValueError, ValidationError) as exc:
                problem = f"the descriptor stamped on {name!r} is not one the workbench reads: {exc}"
    override = descriptor_dir / f"{dataset_id}.json"
    if override.resolve().parent != descriptor_dir.resolve():
        raise UnknownDatasetError(f"dataset id {dataset_id!r} names no file of the descriptor directory")
    declared = Declared.model_validate_json(override.read_text()) if override.exists() else stamped
    if transient and declared is not None:
        problem = "a table could not be read right now, so its descriptor is not used until it can be"
        declared = None
    elif declared is not None and (problems := validate_descriptor(declared, tables)):
        problem = "its descriptor names what the dataset lacks: " + "; ".join(problems)
        declared = None
    if problem is not None:
        logger.warning("dataset %s is shown as a plain table: %s", dataset_id, problem)
    return DatasetDescriptor(
        id=dataset_id,
        source=source.name,
        kind=source.kind,
        tables=tables,
        declared=declared,
        problem=problem,
        unreadable=unreadable,
        read_only=annotatable(source),
        transient=transient,
    )


class Survey(BaseModel):
    """One source as a listing shows it: what discovery found, the datasets it opened, and what it could not open."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    discovery: SourceDiscovery
    opened: list[DatasetDescriptor] = Field(default_factory=list)


#: The threads sources are surveyed on, each source on its own, so one that hangs delays and hides no other.
_SURVEYS: Final = ThreadPoolExecutor(max_workers=8, thread_name_prefix="source-survey")


class DatasetRegistry:
    """The datasets of every declared source, as each caller sees them, with a bounded cache of the handles opened."""

    def __init__(self, settings: MediaSettings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        #: The one clock the registry reads (a survey's deadline, `ABSENT_RECHECK_S`): monotonic, a test's own where it
        #: names one. A handle's own reuse of an open is measured against the vend's expiry, a wall-clock time
        #: (`reusable_until`).
        self._clock = clock
        self._handles: OrderedDict[tuple[str, str], DatasetHandle] = OrderedDict()
        #: When a cached handle holding a table with nothing to read is opened again, by (caller, dataset): one entry per
        #: cached handle, dropped with it.
        self._recheck: dict[tuple[str, str], float] = {}
        #: The (caller, dataset)s found holding nothing to read, and until when they are answered so unopened: per caller,
        #: since what a catalog lists is the caller's answer, and at most as many as the handle cache holds, the least
        #: recently found dropped first.
        self._absent: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._sessions: OrderedDict[tuple[str, str], CallerSessions] = OrderedDict()
        self._lock = threading.Lock()
        self._handle_cap = HANDLES_PER_SOURCE * len(settings.sources.items)

    def client(self, source: RaskSource | NamespaceSource | TablesSource, caller: LakehouseCaller) -> LanceNamespace:
        """The namespace client ``source`` is read through for ``caller``: a lakehouse with the token its identity names,
        any other source with none, once the caller may read it (`client_kit.sources.connect_as`)."""
        return connect_as(source, caller)

    def _survey(self, source: RaskSource | NamespaceSource | TablesSource, caller: LakehouseCaller, deadline: float) -> Survey:
        try:
            found = discover(source, self.client(source, caller), deadline, clock=self._clock)
        except Exception as exc:
            logger.warning("source %s could not be listed", source.name, exc_info=True)
            found = SourceDiscovery(
                source=source.name, kind=source.kind, title=source.title, unlisted=[Unlisted(id=qualify(source, []), reason=reason_for(exc))]
            )
        survey = Survey(discovery=found)
        for dataset_id in found.datasets:
            if self._clock() > deadline:
                found.truncated = True
                break
            try:
                survey.opened.append(self.get(dataset_id, caller).descriptor)
            except DatasetAbsentError as exc:
                # Expected, and said where it was found (`load_dataset_descriptor`): no second line, no traceback.
                found.unlisted.append(Unlisted(id=dataset_id, reason=reason_for(exc)))
            except Exception as exc:
                logger.warning("dataset %s could not be opened", dataset_id, exc_info=True)
                found.unlisted.append(Unlisted(id=dataset_id, reason=reason_for(exc)))
        return survey

    def survey(self, caller: LakehouseCaller) -> list[Survey]:
        """The sources the caller is admitted to (``caller.admitted``), each surveyed on its own within
        :data:`DISCOVERY_DEADLINE_S`, in the declaration's order: one that cannot be read, or does not answer in time, is
        reported as such and never hides the others."""
        sources = [source for source in self._settings.sources.items if source.name in caller.admitted]
        deadline = self._clock() + DISCOVERY_DEADLINE_S
        futures: list[Future[Survey]] = []
        for source in sources:
            context = contextvars.copy_context()
            futures.append(_SURVEYS.submit(lambda c=context, s=source: c.run(self._survey, s, caller, deadline)))
        surveys: list[Survey] = []
        for source, future in zip(sources, futures, strict=True):
            try:
                surveys.append(future.result(timeout=max(0.0, deadline - self._clock()) + CALL_TIMEOUT_S))
            except FutureTimeoutError:
                reason = f"it did not answer within {DISCOVERY_DEADLINE_S:.0f} s"
                late = SourceDiscovery(
                    source=source.name, kind=source.kind, title=source.title, truncated=True, unlisted=[Unlisted(id=qualify(source, []), reason=reason)]
                )
                surveys.append(Survey(discovery=late))
        return surveys

    def get(self, dataset_id: str, caller: LakehouseCaller) -> DatasetHandle:
        """The caller's handle on ``dataset_id`` (``<source>~<namespace>``), opened on first use and cached for this caller
        alone; a dataset a table of which could not be read right now is not cached, so the next request reads it again,
        and one holding a table the catalog lists with no version to read is opened again after :data:`ABSENT_RECHECK_S`.

        Raises:
            UnknownDatasetError: The id names no declared source, is not a namespace id the source admits, or the source
                holds no such namespace.
            DatasetAbsentError: No table the catalog lists in it holds a version to read; answered so, unopened, for
                :data:`ABSENT_RECHECK_S`.
            ForbiddenError: The source is the workbench's to decide and the caller is not one of its readers; asked before
                the caller's cached handle is served (`client_kit.sources.require_admitted`).
        """
        try:
            located = self._settings.sources.dataset(dataset_id)
        except ValueError as exc:
            raise UnknownDatasetError(f"invalid dataset id {dataset_id!r}: {exc}") from exc
        require_admitted(located.source, caller)
        key = (caller.subject, dataset_id)
        with self._lock:
            now = self._clock()
            absent_until = self._absent.get(key)
            if absent_until is not None and absent_until > now:
                raise DatasetAbsentError(f"dataset {dataset_id!r}: {LISTED_NOT_WRITTEN}")
            handle = self._handles.get(key)
            if handle is not None and self._recheck.get(key, now + 1) > now:
                self._handles.move_to_end(key)
                return handle
        client = self.client(located.source, caller)
        sessions = self._caller_sessions(located.source.name, caller.subject)
        try:
            with translate_catalog_errors():
                descriptor = load_dataset_descriptor(client, located.source, dataset_id, located.identifier, self._settings.descriptor_dir, sessions.lance)
        except NotFoundError as exc:
            raise UnknownDatasetError(f"dataset {dataset_id!r} not found: {exc}") from exc
        if not descriptor.tables and not descriptor.unreadable:
            raise UnknownDatasetError(f"dataset {dataset_id!r} holds no table")
        holds_absent = LISTED_NOT_WRITTEN in descriptor.unreadable.values()
        if not descriptor.tables and set(descriptor.unreadable.values()) == {LISTED_NOT_WRITTEN}:
            # Nothing in it can be read: the dataset is answered as absent, and not opened again for a while.
            with self._lock:
                self._handles.pop(key, None)
                self._recheck.pop(key, None)
                self._absent[key] = self._clock() + ABSENT_RECHECK_S
                self._absent.move_to_end(key)
                while len(self._absent) > self._handle_cap:
                    self._absent.popitem(last=False)
            raise DatasetAbsentError(f"dataset {dataset_id!r}: {LISTED_NOT_WRITTEN}")
        # The lancedb connection shares the namespace client, so lancedb's opens carry the same token and trace context.
        connection = LanceNamespaceDBConnection(namespace_client=client, session=sessions.lancedb, read_consistency_interval=timedelta(0))
        handle = DatasetHandle(
            id=dataset_id,
            source=located.source,
            namespace=located.identifier,
            descriptor=descriptor,
            client=client,
            connection=connection,
            sessions=sessions,
        )
        if descriptor.transient:
            return handle
        with self._lock:
            self._absent.pop(key, None)
            self._handles[key] = handle
            self._handles.move_to_end(key)
            # A dataset holding a table with nothing to read is opened again after a while, so a table written since is
            # seen; one whose every table opened is kept until it is evicted.
            if holds_absent:
                self._recheck[key] = self._clock() + ABSENT_RECHECK_S
            else:
                self._recheck.pop(key, None)
            while len(self._handles) > self._handle_cap:
                evicted, _ = self._handles.popitem(last=False)
                self._recheck.pop(evicted, None)
        logger.info("opened dataset %s (%d tables)", dataset_id, len(descriptor.tables))
        return handle

    def _caller_sessions(self, source: str, caller: str) -> CallerSessions:
        """``caller``'s sessions on ``source``, made on first use; the least recently used are dropped past
        :data:`CALLER_SESSIONS` per source (a handle that still holds them keeps them alive).

        Per source as well as per caller: a session reuses a live store for a location whatever credential the next open
        carries, so a store one source's catalog vended is never reused for another source's table at the same place.
        """
        key = (source, caller)
        with self._lock:
            sessions = self._sessions.get(key)
            if sessions is None:
                sessions = self._sessions[key] = CallerSessions.fresh()
            self._sessions.move_to_end(key)
            while len(self._sessions) > CALLER_SESSIONS * len(self._settings.sources.items):
                self._sessions.popitem(last=False)
            return sessions

    def default(self, caller: LakehouseCaller) -> DatasetHandle:
        """The caller's handle on the configured default dataset.

        Raises:
            UnknownDatasetError: No default dataset is configured.
        """
        if self._settings.default_dataset is None:
            raise UnknownDatasetError("the request names no dataset and WORKBENCH_DEFAULT_DATASET is not set")
        return self.get(self._settings.default_dataset, caller)
