"""The datasets of every declared source, each one's facts and descriptor, and any table's rows.

``GET /api/datasets`` lists, source by source, every dataset the caller can see: a namespace that holds tables, at any
depth, with its tables' facts and whether it carries a workbench descriptor; what could not be listed or opened is
listed too, with the reason, never skipped in silence. ``GET /api/datasets/{id}`` is one dataset's facts (a dataset with
no descriptor is shown from them, as a plain table), ``GET /api/datasets/{id}/descriptor`` the merged descriptor the
stamped views render from, and ``GET /api/datasets/{id}/tables/{table}/rows`` (and ``.../rows/{rowid}``) any table's
rows from its schema alone (`viewer.services.plain_rows`).
"""

import logging
from typing import Annotated

import lance
from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from client_kit.lancekit.descriptor import DatasetDescriptor
from client_kit.lancekit.introspect import TableInfo
from client_kit.lancekit.registry import DatasetHandle, Survey, Unlisted
from client_kit.media.deps import StateDep
from client_kit.media.state import dataset_handle, ensure_registry
from service_kit.exceptions import NotFoundError
from viewer.api.security import AUTHENTICATED, READS_DATASET, CallerDep
from viewer.services.derived_views import VECTOR_MAX_DIM
from viewer.services.plain_rows import (
    MAX_OFFSET,
    MAX_PAGE,
    Row,
    RowsPage,
    RowsQuery,
    SortOrder,
    at_version,
    read_page,
    read_row,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["datasets"])


class TableFacts(BaseModel):
    """The discovered shape of one table, as listed per dataset."""

    row_count: int
    version: int
    n_columns: int


class DatasetSummary(BaseModel):
    """One dataset as the list shows it."""

    id: str
    source: str
    #: Whether the dataset carries a workbench descriptor; without one it is shown as a plain table.
    stamped: bool
    tables: dict[str, TableFacts]
    capabilities: list[str] = Field(default_factory=list)
    #: Why a descriptor the dataset carries could not be used.
    problem: str | None = None
    #: The tables listed here that could not be opened, each with why.
    unreadable: dict[str, str] = Field(default_factory=dict)
    #: Why the dataset cannot be annotated, or ``None`` when it can.
    read_only: str | None = None


class SourceListing(BaseModel):
    """One source and the datasets the caller can see in it."""

    name: str
    kind: str
    title: str
    datasets: list[DatasetSummary]
    #: Namespaces (or the whole source) whose listing failed, and datasets that were listed and could not be opened.
    unlisted: list[Unlisted] = Field(default_factory=list)
    #: The walk stopped at its bounds before seeing every namespace.
    truncated: bool = False


class DatasetsResponse(BaseModel):
    sources: list[SourceListing]


class SetupLimits(BaseModel):
    """What a person's set-up of a table's views can name, from the reads that draw them (`viewer.services.derived_views`):
    the app offers no set-up these reads would refuse."""

    vector_max_dim: int = VECTOR_MAX_DIM


class DatasetFacts(BaseModel):
    """One dataset as its page reads it: its source, its tables in full, and what of the workbench applies to it."""

    id: str
    source: str
    kind: str
    stamped: bool
    tables: dict[str, TableInfo]
    capabilities: list[str] = Field(default_factory=list)
    problem: str | None = None
    unreadable: dict[str, str] = Field(default_factory=dict)
    read_only: str | None = None
    setup_limits: SetupLimits = Field(default_factory=SetupLimits)


def _capabilities(descriptor: DatasetDescriptor) -> list[str]:
    declared = descriptor.declared
    return [] if declared is None else [name for name in declared.capabilities if descriptor.capability_available(name)]


def _summary(descriptor: DatasetDescriptor) -> DatasetSummary:
    return DatasetSummary(
        id=descriptor.id,
        source=descriptor.source,
        stamped=descriptor.declared is not None,
        tables={name: TableFacts(row_count=info.row_count, version=info.version, n_columns=len(info.columns)) for name, info in descriptor.tables.items()},
        capabilities=_capabilities(descriptor),
        problem=descriptor.problem,
        unreadable=descriptor.unreadable,
        read_only=descriptor.read_only,
    )


def _listing(survey: Survey) -> SourceListing:
    found = survey.discovery
    return SourceListing(
        name=found.source,
        kind=found.kind,
        title=found.title,
        datasets=[_summary(descriptor) for descriptor in survey.opened],
        unlisted=list(found.unlisted),
        truncated=found.truncated,
    )


@router.get("/datasets", dependencies=[AUTHENTICATED])
async def list_datasets(caller: CallerDep, state: StateDep) -> DatasetsResponse:
    """Every source the deployment declares that the caller may read, with the datasets the caller can see in each.

    A lakehouse read as the person lists what it lets the caller read, on their own token; any other source is listed
    only to its declared readers (`client_kit.media.source_gate`), with what its catalog holds. Each source is surveyed on
    its own, within a deadline; a dataset that cannot be opened, and a source or namespace that cannot be listed or did
    not answer in time, is reported with a short reason.
    """
    # Off the loop: listing and opening are catalog calls and manifest reads, and this is the first call every page makes.
    surveys = await run_in_threadpool(ensure_registry(state).survey, caller)
    return DatasetsResponse(sources=[_listing(survey) for survey in surveys])


@router.get("/datasets/{dataset_id}", dependencies=[READS_DATASET])
async def dataset_facts(caller: CallerDep, dataset_id: str, state: StateDep) -> DatasetFacts:
    """One dataset's facts, read as the caller; 404 for an id no source holds."""
    descriptor = (await run_in_threadpool(dataset_handle, state, dataset_id, caller)).descriptor
    return DatasetFacts(
        id=descriptor.id,
        source=descriptor.source,
        kind=descriptor.kind,
        stamped=descriptor.declared is not None,
        tables=descriptor.tables,
        capabilities=_capabilities(descriptor),
        problem=descriptor.problem,
        unreadable=descriptor.unreadable,
        read_only=descriptor.read_only,
    )


@router.get("/datasets/{dataset_id}/descriptor", dependencies=[READS_DATASET])
async def dataset_descriptor(caller: CallerDep, dataset_id: str, state: StateDep) -> DatasetDescriptor:
    """The full merged descriptor (discovered tables + declared roles), read as the caller; 404 via the domain handler
    for an unknown id or one that declares no descriptor, and the lakehouse's own refusal for one it will not open."""
    handle = await run_in_threadpool(dataset_handle, state, dataset_id, caller)
    if handle.descriptor.declared is None:
        raise NotFoundError(f"dataset {dataset_id!r} declares no workbench descriptor: it is shown as a plain table")
    return handle.descriptor


TableParam = Annotated[str, Path(min_length=1, max_length=256)]


def table_at(handle: DatasetHandle, table: str, version: int | None) -> tuple[lance.LanceDataset, TableInfo]:
    info = handle.descriptor.tables.get(table)
    if info is None:
        raise NotFoundError(f"table {table!r} missing from dataset {handle.id!r}")
    return at_version(handle.dataset(table), version), info


@router.get("/datasets/{dataset_id}/tables/{table}/rows", dependencies=[READS_DATASET])
async def table_rows(
    caller: CallerDep,
    state: StateDep,
    dataset_id: str,
    table: TableParam,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    sort: Annotated[str | None, Query(max_length=256)] = None,
    order: SortOrder = SortOrder.ASC,
    q: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    column: Annotated[str | None, Query(max_length=256)] = None,
    near: Annotated[int | None, Query(ge=0)] = None,
    vector: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> RowsPage:
    """One page of a table's rows, from its schema alone, read as the caller: in table order or sorted, a full-text
    search's hits where a full-text index is, or the rows most like one where a vector index is (400 otherwise)."""
    query = RowsQuery(
        offset=offset,
        limit=limit,
        sort=sort,
        order=order,
        q=q,
        column=column,
        near=near,
        vector=vector,
        version=version,
    )

    def _read() -> RowsPage:
        handle = dataset_handle(state, dataset_id, caller)
        ds, info = table_at(handle, table, query.version)
        return read_page(ds, info, handle.qualified_table(table), query)

    return await run_in_threadpool(_read)


@router.get("/datasets/{dataset_id}/tables/{table}/rows/{rowid}", dependencies=[READS_DATASET])
async def table_row(
    caller: CallerDep,
    state: StateDep,
    dataset_id: str,
    table: TableParam,
    rowid: Annotated[int, Path(ge=0)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Row:
    """One row in full, at the version its page was read at, with the media type of each binary cell that names one."""

    def _read() -> Row:
        ds, info = table_at(dataset_handle(state, dataset_id, caller), table, version)
        return read_row(ds, info, rowid)

    return await run_in_threadpool(_read)
