"""The doors a person's set-up of a table nobody stamped reads through (`viewer.services.derived_views`).

Under ``/api/datasets/{id}/tables/{table}/``: ``profile`` is what a sample says of each column, which the set-up forms
suggest from; ``atlas`` the points the picked columns place (one Arrow IPC stream, as the stamped atlas's); ``graph``
the nodes and edges they link and ``tree`` the hierarchy they nest, each with what the read saw and cut;
``graph/check`` and ``tree/check`` what the picked columns hold in the profile's sample alone, which the form shows
before anything is drawn; and ``graph/rows`` and ``tree/rows`` the rows one node stands for, decided by the same code
that counted them. ``/api/datasets/{id}/documents/{doc}/peaks`` is a stamped dataset's document's waveform, decoded
from its media blob (`viewer.services.peaks`): kept and bounded the same way, a heavy build like a whole view.

Each is a read of the table as the caller, behind the same gate as the plain rows (`READS_DATASET`: a verified person
who may read the source; a lakehouse decides on the person's own token), at the version named or the latest. The table
is opened as the caller first, so a kept answer is reached only behind the caller's own read. An answer is kept per
(dataset, table, version, read, the columns picked) within the byte budget it shares with the stamped atlas's points
(`workbench.media.cache_bounds.keep_shared`), built once however many ask for it cold. A cold build of a whole view
takes one of the viewer's derived build slots (`viewer.services.build_slots`; ``VIEWER_DERIVED_BUILDS``) or is refused
with 503 and Retry-After; a build every caller has left stops at its next check and keeps nothing.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated

import av
import lance
import numpy as np
from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from viewer.api.security import READS_DATASET, CallerDep, SettingsDep
from viewer.api.v1.endpoints.datasets import TableParam, table_at
from viewer.core.config import ViewerSettings, viewer_settings
from viewer.services.build_slots import BuildSlot, BuildSlots
from viewer.services.derived_views import (
    PROFILE_ROWS,
    Abandoned,
    AtlasSpec,
    Budget,
    DerivedGraph,
    DerivedTree,
    GraphNodeQuery,
    GraphSpec,
    NodeRows,
    Picks,
    ReadExtent,
    TreeNodeQuery,
    TreeSpec,
    budget,
    build_atlas,
    build_graph,
    build_tree,
    graph_members,
    name_graph,
    name_tree,
    profile_table,
    strict_json,
    tree_json,
    tree_members,
)
from viewer.services.peaks import peaks_of
from viewer.services.plain_rows import ColumnView, columns_of, page_of_rowids
from workbench.arrow_ipc import ARROW_STREAM_MEDIA_TYPE
from workbench.exceptions import DomainError, NotFoundError, ServiceUnavailableError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.blobs import is_blob_field
from workbench.lancekit.introspect import TableInfo
from workbench.lancekit.keys import validate_doc_key
from workbench.lancekit.predicate import eq
from workbench.media.cache_bounds import keep_shared
from workbench.media.deps import StateDep
from workbench.media.state import AppState, dataset_handle


router = APIRouter(prefix="/api/datasets", tags=["datasets"])

#: The most answers kept at once (the lookup bound); their bytes share the viewer's `cache_bytes` with the atlas's points.
_CACHE_ENTRIES = 48
#: How often a caller waiting on a build asks whether its client is still there, in seconds.
_POLL_SECONDS = 0.5

#: What every answer here carries: kept by the person's browser alone, and asked again on each use.
_PRIVATE = {"Cache-Control": "private, no-cache"}

type Build = Callable[[lance.LanceDataset, list[ColumnView], Budget], bytes]
type Key = tuple[str, str, int, str, str]


class _Flight(BaseModel):
    """One build in progress: the callers waiting on it, and the event that stops it once none is."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: asyncio.Task[bytes]
    cancelled: threading.Event
    waiters: int = 0


class _Opened(BaseModel):
    """The table a request reads, opened as its caller."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ds: lance.LanceDataset
    info: TableInfo
    columns: list[ColumnView]
    qualified: str
    dataset: str


class UnreadableMedia(DomainError):
    """A document's media is not where a waveform can be decoded from, or is in no container FFmpeg reads."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unprocessable Content"
    problem_type = "about:blank#unreadable-media"


class _Gone(ServiceUnavailableError):
    """The caller's client went away while it waited: nobody reads this answer."""


def _slots(state: AppState, settings: ViewerSettings) -> BuildSlots:
    if not isinstance(state.derived_slots, BuildSlots):
        state.derived_slots = BuildSlots("derived view", settings.derived_builds)
    return state.derived_slots


async def _open(state: AppState, caller: LakehouseCaller, dataset_id: str, table: str, version: int | None) -> _Opened:
    def opened() -> _Opened:
        handle = dataset_handle(state, dataset_id, caller)
        ds, info = table_at(handle, table, version)
        return _Opened(ds=ds, info=info, columns=columns_of(ds.schema, info), qualified=handle.qualified_table(table), dataset=handle.id)

    return await run_in_threadpool(opened)


async def _answer(
    request: Request,
    state: AppState,
    settings: ViewerSettings,
    table: _Opened,
    read: str,
    picks: str,
    build: Build,
    *,
    heavy: bool,
) -> bytes:
    """``build``'s answer for the opened table, kept per key and built once; a ``heavy`` build takes a slot first.

    Raises:
        BuildsBusy: A heavy build is cold and every slot is taken (503 with Retry-After).
    """
    key: Key = (table.dataset, table.info.name, int(table.ds.version), read, picks)
    entry = state.derived_cache.pop(key, None)
    if entry is not None:
        state.derived_cache[key] = entry
        return entry[0]
    flight = state.derived_flights.get(key)
    if not isinstance(flight, _Flight) or flight.cancelled.is_set():
        slot = _slots(state, settings).acquire() if heavy else None
        cancelled = threading.Event()
        task = asyncio.ensure_future(_build(state, key, lambda: build(table.ds, table.columns, budget(cancelled)), slot))
        # A build every caller abandoned still ends, and its failure is read here rather than reported as never retrieved.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        flight = _Flight(task=task, cancelled=cancelled)
        state.derived_flights[key] = flight
    return await _wait(flight, request)


async def _wait(flight: _Flight, request: Request) -> bytes:
    """``flight``'s answer, for as long as the caller's client is there; the last caller to leave stops the build."""
    flight.waiters += 1
    try:
        while True:
            done, _ = await asyncio.wait({flight.task}, timeout=_POLL_SECONDS)
            if done:
                try:
                    return flight.task.result()
                except Abandoned as exc:
                    raise ServiceUnavailableError("this view was being drawn for a caller who left; ask again", headers={"Retry-After": "1"}) from exc
            if await request.is_disconnected():
                raise _Gone("the caller went away")
    finally:
        flight.waiters -= 1
        if flight.waiters == 0 and not flight.task.done():
            flight.cancelled.set()


async def _build(state: AppState, key: Key, run: Callable[[], bytes], slot: BuildSlot | None) -> bytes:
    try:
        body = await run_in_threadpool(run)
        keep_shared(
            state.derived_cache, key, body, others=[state.points_cache], max_entries=_CACHE_ENTRIES, max_bytes=viewer_settings(state.settings).cache_bytes
        )
        return body
    finally:
        if slot is not None:
            slot.release()
        current = state.derived_flights.get(key)
        if isinstance(current, _Flight) and current.task is asyncio.current_task():
            state.derived_flights.pop(key, None)


def _json(body: bytes) -> Response:
    return Response(content=body, media_type="application/json", headers=_PRIVATE)


def _members(rowids: list[int], extent: ReadExtent) -> bytes:
    """A node's rows as kept: the read's extent as one line of JSON, then the row ids as int64."""
    return extent.model_dump_json().encode() + b"\n" + np.asarray(rowids, dtype=np.int64).tobytes()


def _unmembers(body: bytes) -> tuple[list[int], ReadExtent]:
    head, _, rest = body.partition(b"\n")
    return np.frombuffer(rest, dtype=np.int64).tolist(), ReadExtent.model_validate_json(head)


async def _page(opened: _Opened, body: bytes, offset: int, limit: int) -> NodeRows:
    rowids, extent = _unmembers(body)
    page = await run_in_threadpool(page_of_rowids, opened.ds, opened.info, opened.qualified, rowids, offset, limit)
    return NodeRows(**page.model_dump(), extent=extent)


@router.get("/{dataset_id}/tables/{table}/profile", dependencies=[READS_DATASET])
async def table_profile(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, picks: Annotated[Picks, Query()]
) -> Response:
    """What a sample of the table says of each column (its kind, distinct values, nulls, a path's separator, the key
    columns its values are found among, whether a vector can be projected), for the set-up forms to suggest from.
    Answers ``TableProfile``."""
    opened = await _open(state, caller, dataset_id, table, picks.version)

    def build(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return strict_json(profile_table(ds, columns, table, within=within))

    return _json(await _answer(request, state, settings, opened, "profile", "", build, heavy=False))


@router.get("/{dataset_id}/tables/{table}/atlas", dependencies=[READS_DATASET])
async def table_atlas(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, spec: Annotated[AtlasSpec, Query()]
) -> Response:
    """The points the picked columns place, as one Arrow IPC stream: float16 ``x``/``y`` in ``[-1, 1]``, each point's
    ``rowid``, and a ``color`` and a ``label`` where picked; a vector column is projected to two dimensions. The
    schema's metadata says how much of the table was read, sampled and left out. 400 for a pick the table cannot
    answer, 503 with Retry-After while every build slot is taken."""
    opened = await _open(state, caller, dataset_id, table, spec.version)

    def build(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return build_atlas(ds, columns, spec, within=within)

    body = await _answer(request, state, settings, opened, "atlas", spec.cache_key(), build, heavy=True)
    return Response(content=body, media_type=ARROW_STREAM_MEDIA_TYPE, headers=_PRIVATE)


@router.get("/{dataset_id}/tables/{table}/graph", dependencies=[READS_DATASET])
async def table_graph(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, spec: Annotated[GraphSpec, Query()]
) -> Response:
    """The nodes and edges the picked columns link (an edge table's from and to, or a self-referencing table's id and
    points-to), the heaviest kept, each node with its typed key and the rows naming it, and what the rows read say of
    them. The links are built once for every label and colour, which are read for the nodes drawn alone.
    Answers ``DerivedGraph``; 400 for a pick the table cannot answer, 503 with Retry-After while every slot is taken."""
    opened = await _open(state, caller, dataset_id, table, spec.version)

    def structure(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return strict_json(build_graph(ds, columns, spec, within=within))

    graph = DerivedGraph.model_validate_json(await _answer(request, state, settings, opened, "graph", spec.structure_key(), structure, heavy=True))

    def named(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        within.check()
        return strict_json(name_graph(ds, columns, spec, graph))

    return _json(await _answer(request, state, settings, opened, "graph-named", spec.cache_key(), named, heavy=False))


@router.get("/{dataset_id}/tables/{table}/graph/check", dependencies=[READS_DATASET])
async def table_graph_check(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, spec: Annotated[GraphSpec, Query()]
) -> Response:
    """What the picked columns hold in the profile's sample alone (``PROFILE_ROWS`` rows): the graph's diagnostics, which
    the set-up form shows before anything is drawn. Answers ``GraphDiagnostics``."""
    opened = await _open(state, caller, dataset_id, table, spec.version)

    def check(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return strict_json(build_graph(ds, columns, spec, within=within, cap=PROFILE_ROWS).diagnostics)

    return _json(await _answer(request, state, settings, opened, "graph-check", spec.structure_key(), check, heavy=False))


@router.get("/{dataset_id}/tables/{table}/graph/rows", dependencies=[READS_DATASET])
async def table_graph_rows(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, query: Annotated[GraphNodeQuery, Query()]
) -> NodeRows:
    """One page of the rows that name the graph's node ``key`` (as its from or to, or its id or points-to), in the read
    the graph was drawn from: the rows the node's count counted. Answers ``NodeRows``."""
    opened = await _open(state, caller, dataset_id, table, query.version)
    spec = GraphSpec.model_validate(query.model_dump(exclude={"key", "offset", "limit"}))

    def members(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return _members(*graph_members(ds, columns, spec, query.key, within=within))

    body = await _answer(request, state, settings, opened, "graph-rows", f"{spec.structure_key()}\x00{query.key}", members, heavy=True)
    return await _page(opened, body, query.offset, query.limit)


@router.get("/{dataset_id}/tables/{table}/tree", dependencies=[READS_DATASET])
async def table_tree(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, spec: Annotated[TreeSpec, Query()]
) -> Response:
    """The hierarchy the picked columns nest (an id and a parent, or one path), its largest branches kept, each node with
    its key and the rows at or below it, with its orphans, cycles and cuts counted. Answers ``DerivedTree``; 400 for a
    pick the table cannot answer, 503 with Retry-After while every slot is taken."""
    opened = await _open(state, caller, dataset_id, table, spec.version)

    def structure(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return tree_json(build_tree(ds, columns, spec, within=within))

    tree = DerivedTree.model_validate_json(await _answer(request, state, settings, opened, "tree", spec.structure_key(), structure, heavy=True))

    def named(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        within.check()
        return tree_json(name_tree(ds, columns, spec, tree))

    return _json(await _answer(request, state, settings, opened, "tree-named", spec.cache_key(), named, heavy=False))


@router.get("/{dataset_id}/tables/{table}/tree/check", dependencies=[READS_DATASET])
async def table_tree_check(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, spec: Annotated[TreeSpec, Query()]
) -> Response:
    """What the picked columns hold in the profile's sample alone (``PROFILE_ROWS`` rows): the tree's diagnostics, which
    the set-up form shows before anything is drawn. Answers ``TreeDiagnostics``."""
    opened = await _open(state, caller, dataset_id, table, spec.version)

    def check(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return strict_json(build_tree(ds, columns, spec, within=within, cap=PROFILE_ROWS).diagnostics)

    return _json(await _answer(request, state, settings, opened, "tree-check", spec.structure_key(), check, heavy=False))


@router.get("/{dataset_id}/tables/{table}/tree/rows", dependencies=[READS_DATASET])
async def table_tree_rows(
    request: Request, caller: CallerDep, state: StateDep, settings: SettingsDep, dataset_id: str, table: TableParam, query: Annotated[TreeNodeQuery, Query()]
) -> NodeRows:
    """One page of the rows the tree's node ``key`` stands for (the rows at or below it; a "(itself)" leaf's, the rows
    at it alone), in the read the tree was drawn from: the rows the node's count counted. Answers ``NodeRows``."""
    opened = await _open(state, caller, dataset_id, table, query.version)
    spec = TreeSpec.model_validate(query.model_dump(exclude={"key", "offset", "limit"}))
    key = query.tree_key()

    def members(ds: lance.LanceDataset, columns: list[ColumnView], within: Budget) -> bytes:
        return _members(*tree_members(ds, columns, spec, key, within=within))

    body = await _answer(request, state, settings, opened, "tree-rows", f"{spec.structure_key()}\x00{key.encoded()}", members, heavy=True)
    return await _page(opened, body, query.offset, query.limit)


@router.get("/{dataset_id}/documents/{doc_id}/peaks", dependencies=[READS_DATASET])
async def document_peaks(
    request: Request,
    caller: CallerDep,
    state: StateDep,
    settings: SettingsDep,
    dataset_id: str,
    doc_id: Annotated[str, Path(min_length=1, max_length=256)],
) -> Response:
    """The waveform of a document's sound (a recording, or a video's soundtrack): each bucket's min and max, decoded once
    from the document's media blob as a stream and kept per (dataset, documents table, version, document)
    (`viewer.services.peaks`). Read as the caller like every door here, the document id checked against the dataset's
    identity before any read. Answers ``Peaks``; 404 for a dataset with no documents table, a document it does not hold
    or an empty media cell, 422 for media in a plain binary column or in no container FFmpeg reads, 503 with Retry-After
    while every build slot is taken."""

    def located() -> tuple[str, str, str, str]:
        handle = dataset_handle(state, dataset_id, caller)
        binding = handle.declared.document
        if binding is None:
            raise NotFoundError("the dataset declares no documents table")
        return binding.table, binding.media_blob, handle.declared.identity.doc_key, validate_doc_key(handle.declared, doc_id)

    table, column, doc_key, doc = await run_in_threadpool(located)
    opened = await _open(state, caller, dataset_id, table, None)

    def build(ds: lance.LanceDataset, _columns: list[ColumnView], within: Budget) -> bytes:
        if not is_blob_field(ds.schema.field(column)):
            raise UnreadableMedia(f"the media column {column!r} is not a blob column: a waveform is decoded from a blob, read in ranges")
        found = ds.to_table(columns=[], filter=eq(doc_key, doc), with_row_id=True, limit=1)
        if found.num_rows == 0:
            raise NotFoundError(f"no document {doc!r}")
        blob = ds.take_blobs(column, ids=[int(found.column("_rowid")[0].as_py())])[0]
        if blob is None:
            raise NotFoundError(f"document {doc!r} holds no {column!r} payload")

        def still_wanted() -> None:
            if within.cancelled.is_set():
                raise Abandoned

        try:
            with blob:
                return peaks_of(blob, check=still_wanted).model_dump_json().encode()
        except av.FFmpegError as exc:
            raise UnreadableMedia(f"document {doc!r}'s media could not be decoded: {exc}") from exc

    return _json(await _answer(request, state, settings, opened, "peaks", doc, build, heavy=True))
