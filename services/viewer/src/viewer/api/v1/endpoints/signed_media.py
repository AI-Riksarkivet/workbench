"""Signed media URLs: a batch of them for the person (``POST /api/signed-media``), and the cell each one serves
(``GET /api/signed-media/{token}``).

The batch door is a door like any other: a verified person, and every cell it names resolved as that person, from their
own catalog read of the table at the version it names, so a cell the catalog does not let them read is never named.
The link is then the permission (owner decision on #9): the GET reads exactly the signed cell, at the signed version, as
the viewer's own identity on its client-credentials token (`service_kit.governed.deps.lakehouse_service_caller`), for as
long as the signature is valid (`viewer.services.signed_media`). No person's token is held for it, so any replica serves
it, and the workbench's client must hold a read grant on what its people may sign.

Every answer is sandboxed (``Content-Security-Policy: sandbox; default-src 'none'``, ``X-Content-Type-Options:
nosniff``), and a stored type a browser would run is served as an attachment (`media.served_media_type`). The response
is ``Cache-Control: private, max-age=<the URL's remaining life>, immutable`` with a strong ETag of (table,
version, row, column), and a ``Range`` is a 206. ``private`` because each serve asks again whether the person the URL was
signed for may still read its source: a shared cache would go on serving the bytes to anyone holding the URL after that
person stopped being a reader, and only the browser that asked keeps them.

Every cell is served here, External blobs too: presigning an External blob's URI would hand the browser a store URL,
which needs the store reachable from browsers and a raw S3 client this repository bans.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from functools import partial
from typing import Annotated, Literal

import lance
import pyarrow as pa
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from client_kit.lancekit.blobs import is_blob_field
from client_kit.lancekit.keys import validate_doc_key
from client_kit.lancekit.predicate import eq
from client_kit.lancekit.registry import DatasetHandle, reason_for, table_dataset
from client_kit.media.deps import StateDep
from client_kit.media.source_gate import may_read, require_read
from client_kit.media.state import AppState, dataset_handle
from client_kit.sources import connect_source
from service_kit.exceptions import ForbiddenError, NotFoundError
from service_kit.governed.deps import lakehouse_service_caller
from service_kit.governed.token_exchange import LakehouseCaller
from viewer.api.security import CallerDep, CheckerDep, CurrentSubject, SettingsDep
from viewer.api.v1.endpoints.media import ByteRange, RangeVerdict, frame_row, parse_range, served_media_type
from viewer.api.v1.endpoints.pages import open_page_table, sniff_media_type
from viewer.services import signed_media
from viewer.services.plain_rows import at_version
from viewer.services.signed_media import ServiceReads, SignedBlob


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signed-media", tags=["media"])

#: The most URLs one batch asks for: a page of thumbnails, not a corpus.
MAX_BATCH = 200
_READ_CHUNK = 1 << 20
#: How many leading bytes name an unlabelled payload's type (`pages.sniff_media_type`).
_SNIFF_BYTES = 12
#: The largest plain binary cell a URL is signed for. Such a cell has no take path and no length short of its bytes
#: (`pages._page_rows` measured it), so a GET reads it whole; signing refuses one past this, and since a signed version
#: is immutable no GET holds more. Blob cells stream in bounded ranges and have no such limit.
MAX_PLAIN_CELL_BYTES = 16 * 1024 * 1024
#: What every answer of the GET door carries besides its type's own headers: a payload a writer chose is never a
#: document that runs anything, even when opened directly (CSP Level 3 § sandbox).
_SANDBOXED = {"Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"}


class DocumentItem(BaseModel):
    """A document's media or thumbnail, in the request's dataset."""

    role: Literal["media", "thumbnail"]
    doc_id: str


class FrameItem(BaseModel):
    """A chunk's frame, in the request's dataset: the key fields after the doc key, in the descriptor's order."""

    role: Literal["frame"]
    doc_id: str
    keys: list[int] = Field(default_factory=list, max_length=8)
    frame_idx: int = Field(default=0, ge=0)


class PageItem(BaseModel):
    """A page's payload in a page table, named by its catalog id (``acme$media$pages``) and its ``id`` column."""

    role: Literal["page"]
    table: str = Field(min_length=1)
    page_id: int = Field(ge=0)


class CellItem(BaseModel):
    """Any table's binary or blob cell, by its qualified table name, column, Lance row id and the version its row was
    read at: what a plain table's row detail previews (`viewer.services.plain_rows`)."""

    role: Literal["cell"]
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)
    row: int = Field(ge=0)
    version: int = Field(ge=1)


type MediaItem = Annotated[DocumentItem | FrameItem | PageItem | CellItem, Field(discriminator="role")]


class MediaUrlRequest(BaseModel):
    #: The dataset documents and frames are in; a request naming only pages needs none.
    dataset: str | None = None
    items: list[MediaItem] = Field(min_length=1, max_length=MAX_BATCH)


class MediaUrl(BaseModel):
    """One item's answer: its URL and when that stops serving (Unix seconds), or why there is none."""

    url: str | None = None
    expires_at: int | None = None
    detail: str | None = None


class MediaUrls(BaseModel):
    """One answer per requested item, in the request's order."""

    urls: list[MediaUrl]


class _Cell(BaseModel):
    source: str
    table: list[str]
    column: str
    row: int
    version: int
    mime: str | None


def _document_cell(handle: DatasetHandle, item: DocumentItem) -> _Cell:
    declared = handle.declared
    binding = declared.document
    if binding is None:
        raise NotFoundError("the dataset declares no documents table")
    if item.role == "media":
        column, mime_column, default_mime = binding.media_blob, binding.mime, "application/octet-stream"
    elif binding.thumbnail is None:
        raise NotFoundError("the dataset declares no thumbnails")
    else:
        column, mime_column, default_mime = binding.thumbnail, binding.thumbnail_mime, "image/jpeg"
    ds = table_dataset(handle, binding.table)
    # The blob column at default handling is its descriptor (`lance_docs/lance/format/table/blob.md` § Data File Schema):
    # one scan answers the row id and whether the cell holds a payload, and reads no payload byte.
    columns = [column] + ([mime_column] if mime_column else [])
    found = ds.to_table(columns=columns, filter=eq(declared.identity.doc_key, validate_doc_key(declared, item.doc_id)), with_row_id=True, limit=1)
    if found.num_rows == 0:
        raise NotFoundError("doc_id not found")
    if not found.column(column)[0].is_valid:
        raise NotFoundError("no payload for doc_id")
    mime = found.column(mime_column)[0].as_py() if mime_column else None
    return _Cell(
        source=handle.source.name,
        table=handle.table_id(binding.table),
        column=column,
        row=int(found.column("_rowid")[0].as_py()),
        version=int(ds.version),
        mime=mime or default_mime,
    )


def _frame_cell(handle: DatasetHandle, item: FrameItem) -> _Cell:
    frame = frame_row(handle, item.doc_id, tuple(item.keys), item.frame_idx)
    return _Cell(
        source=handle.source.name, table=handle.table_id(frame.table), column=frame.column, row=frame.row, version=int(frame.ds.version), mime=frame.mime
    )


def _page_cell(state: AppState, caller: LakehouseCaller, item: PageItem) -> _Cell:
    ds = open_page_table(state, item.table, caller)
    found = ds.to_table(columns=[], filter=eq("id", item.page_id), with_row_id=True, limit=1)
    if found.num_rows == 0:
        raise NotFoundError(f"no page with id {item.page_id} in {item.table!r}")
    row = int(found.column("_rowid")[0].as_py())
    _require_payload(ds, "payload", row, f"row {item.page_id} in {item.table!r}")
    located = state.settings.sources.table_named(item.table)
    return _Cell(source=located.source.name, table=located.identifier, column="payload", row=row, version=int(ds.version), mime=None)


def _require_payload(ds: lance.LanceDataset, column: str, row: int, what: str) -> None:
    """Refuse (404) a cell that holds nothing, without reading its bytes: a blob cell by its descriptor, a plain binary
    cell by its row alone. Lance reads a plain binary cell only whole, even to test it for null (measured 2026-11-16 on
    pylance 13.0.0: ``IS NOT NULL`` on a 300 MiB cell peaked 634 MiB), so whether it holds bytes, and whether they are
    few enough to serve, is answered by the serve, which has to read them anyway."""
    if is_blob_field(ds.schema.field(column)):
        blob = ds.take_blobs(column, ids=[row])[0]
        if blob is None:
            raise NotFoundError(f"{what} holds no {column!r} payload")
        blob.close()
    elif ds.to_table(columns=[], filter=f"_rowid = {int(row)}", with_row_id=True, limit=1).num_rows == 0:
        raise NotFoundError(f"{what} holds no {column!r} payload")


def _any_cell(state: AppState, caller: LakehouseCaller, item: CellItem) -> _Cell:
    """The cell, read as the person at the version named: its column must hold bytes and its row must be there
    (`_require_payload`)."""
    ds = at_version(open_page_table(state, item.table, caller), item.version)
    if item.column not in ds.schema.names:
        raise NotFoundError(f"{item.table!r} has no column {item.column!r}")
    field = ds.schema.field(item.column)
    if not is_blob_field(field) and not (pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)):
        raise NotFoundError(f"column {item.column!r} of {item.table!r} holds no bytes to serve")
    _require_payload(ds, item.column, item.row, f"row {item.row} of {item.table!r}")
    located = state.settings.sources.table_named(item.table)
    return _Cell(source=located.source.name, table=located.identifier, column=item.column, row=item.row, version=int(ds.version), mime=None)


def _sign_all(state: AppState, caller: LakehouseCaller, reader: str, body: MediaUrlRequest, key: bytes, ttl: int) -> MediaUrls:
    """One answer per item: a URL, or why there is none. One item that cannot be signed (absent, refused, unreadable right
    now) is that item's answer, never the batch's."""
    handle: DatasetHandle | None = None
    expires_at = math.floor(signed_media.now()) + ttl
    urls: list[MediaUrl] = []
    for item in body.items:
        try:
            if isinstance(item, PageItem):
                cell = _page_cell(state, caller, item)
            elif isinstance(item, CellItem):
                cell = _any_cell(state, caller, item)
            else:
                handle = handle or dataset_handle(state, body.dataset, caller)
                cell = _document_cell(handle, item) if isinstance(item, DocumentItem) else _frame_cell(handle, item)
        except NotFoundError as exc:
            urls.append(MediaUrl(detail=exc.detail))
            continue
        except Exception as exc:
            logger.warning("a media item could not be signed", exc_info=True)
            urls.append(MediaUrl(detail=f"this item could not be signed: {reason_for(exc)}"))
            continue
        token = signed_media.sign(SignedBlob(exp=expires_at, reader=reader, **cell.model_dump()), key)
        urls.append(MediaUrl(url=f"{router.prefix}/{token}", expires_at=expires_at))
    return MediaUrls(urls=urls)


@router.post("")
async def sign_media_urls(
    body: MediaUrlRequest, caller: CallerDep, subject: CurrentSubject, check: CheckerDep, state: StateDep, settings: SettingsDep
) -> MediaUrls:
    """Short signed URLs for the cells a page shows, each resolved by the person's own read; an item naming none says why.
    A source the workbench decides for is signed only for one of its readers (403 otherwise)."""
    named = [body.dataset or state.settings.default_dataset or "", *(item.table for item in body.items if isinstance(item, PageItem | CellItem))]
    await require_read(state.settings.sources, check, subject, [name for name in named if name])
    key = signed_media.read_key(settings.media_url_key_file)
    return await run_in_threadpool(_sign_all, state, caller, subject, body, key, settings.media_url_ttl_seconds)


def _service_reads(state: AppState) -> ServiceReads:
    with state.service_reads_lock:
        if state.service_reads is None:
            state.service_reads = ServiceReads()
        reads: ServiceReads = state.service_reads
        return reads


class _OpenCell(BaseModel):
    """The signed cell's bytes as a size, a range reader and a close, and the media type they are stored as."""

    size: int
    read: Callable[[int, int], bytes]
    close: Callable[[], None]
    mime: str


def _open_cell(state: AppState, caller: LakehouseCaller, signed: SignedBlob) -> _OpenCell:
    """Exactly the signed cell, at the signed version, as the viewer's own identity."""
    source = state.settings.sources.named(signed.source)
    client = partial(connect_source, source, person=caller.token, service=caller.service)
    ds: lance.LanceDataset = _service_reads(state).dataset(client, signed.source, signed.table, signed.version)
    if signed.column not in ds.schema.names:
        raise NotFoundError("the media URL names a column the table does not hold")
    if is_blob_field(ds.schema.field(signed.column)):
        blob = ds.take_blobs(signed.column, ids=[signed.row])[0]
        if blob is None:
            raise NotFoundError("no payload for this media URL")
        size, read, close = blob.size(), blob.read_range, blob.close
    else:
        # A plain binary column has no take path and no range: a scan of the one row, by its stable id, reads the cell
        # whole, and one past the cap is not served.
        found = ds.to_table(columns=[signed.column], filter=f"_rowid = {int(signed.row)}", limit=1)
        if found.num_rows == 0 or not found.column(signed.column)[0].is_valid:
            raise NotFoundError("no payload for this media URL")
        payload: bytes = found.column(signed.column)[0].as_py()
        size = len(payload)
        if size > MAX_PLAIN_CELL_BYTES:
            raise NotFoundError(f"this payload is plain binary over {MAX_PLAIN_CELL_BYTES} bytes, too large to serve whole")

        def read(offset: int, length: int) -> bytes:
            return payload[offset : offset + length]

        def close() -> None:
            return None

    try:
        mime = signed.mime or sniff_media_type(read(0, min(_SNIFF_BYTES, size)))
    except BaseException:
        close()
        raise
    return _OpenCell(size=size, read=read, close=close, mime=mime)


def _stream(cell: _OpenCell, start: int, end: int) -> Iterator[bytes]:
    """The inclusive ``[start, end]`` byte range, in bounded reads; the cell is closed when the stream ends or is dropped."""
    try:
        offset = start
        while offset <= end:
            length = min(_READ_CHUNK, end - offset + 1)
            yield cell.read(offset, length)
            offset += length
    finally:
        cell.close()


@router.get("/{token}")
async def serve_signed_media(
    token: str,
    request: Request,
    state: StateDep,
    settings: SettingsDep,
    check: CheckerDep,
    range_header: Annotated[str | None, Header(alias="range")] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """The one cell ``token`` names; its signature is its only credential. Refused (403) when the token is not this viewer's
    signature over what it names, or has expired."""
    signed = signed_media.verify(token, signed_media.read_key(settings.media_url_key_file))
    headers = {
        **_SANDBOXED,
        "ETag": signed.etag,
        "Cache-Control": f"private, max-age={max(0, signed.exp - math.ceil(signed_media.now()))}, immutable",
        "Accept-Ranges": "bytes",
    }
    try:
        source = state.settings.sources.named(signed.source)
    except ValueError as exc:
        raise NotFoundError("the media URL names a source this viewer does not read") from exc
    if not await may_read(state.settings.sources, check, signed.reader, source.name):
        # The link is the permission only while its reader may still read the source the workbench decides for.
        raise ForbiddenError("the person this media URL was signed for may no longer read its source")
    if if_none_match is not None and signed.etag in (tag.strip() for tag in if_none_match.split(",")):
        return Response(status_code=304, headers=headers)
    caller = lakehouse_service_caller(request, settings)
    try:
        cell = await run_in_threadpool(_open_cell, state, caller, signed)
    except OSError as exc:
        # An External blob whose object cannot be read: absent, as the media routes answer it.
        raise NotFoundError("media not available") from exc
    media_type, safety = served_media_type(cell.mime)
    headers = {**headers, **safety}
    rng = parse_range(range_header, cell.size) if range_header else RangeVerdict.IGNORE
    if rng is RangeVerdict.UNSATISFIABLE:
        cell.close()
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{cell.size}"})
    if isinstance(rng, ByteRange):
        return StreamingResponse(
            _stream(cell, rng.start, rng.end),
            status_code=206,
            media_type=media_type,
            headers={**headers, "Content-Length": str(rng.end - rng.start + 1), "Content-Range": f"bytes {rng.start}-{rng.end}/{cell.size}"},
        )
    return StreamingResponse(_stream(cell, 0, cell.size - 1), media_type=media_type, headers={**headers, "Content-Length": str(cell.size)})
