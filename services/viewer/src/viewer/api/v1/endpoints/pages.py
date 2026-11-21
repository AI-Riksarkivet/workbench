"""The document viewer's read path — page images out of a blob-v2 page dataset.

Page datasets store the image bytes in a blob-v2 column (``payload``) alongside the tabular
columns that say which page each one is. Serving them needs two things to be right, and each of them
has already been got wrong once.

READS MUST PRESERVE CARDINALITY. ``read_blobs`` / ``take_blobs`` silently DROP null rows through
pylance 9 (three selected rows with one null payload return two payloads) and from 10.0.0 return
``None`` in that slot instead — so pairing their output POSITIONALLY against a second scan of the
tabular columns misattributes every page after the first gap, with no exception raised. A failed
harvest or a skipped page is exactly that case. Neither route here pairs anything by position: the
listing reads only the blob DESCRIPTORS, which arrive in the same scan as the tabular columns, and
the byte route resolves ``id`` -> stable ``_rowid`` -> ``take_blobs(ids=[rowid])``, where ``None`` in
the slot IS the null signal. ``blobs.py`` says the same in its own words — "the take-path remains
correct for single-row serving (``ids=[rowid]``, where an empty result IS the null signal)". A plain
``large_binary`` payload has neither a take-path nor a descriptor; both routes decide that shape from
``ds.schema`` (see ``_page_rows`` / ``_take_page``).

READS MUST BE BOUNDED BY THE REQUEST, NOT BY THE DATASET. Both routes used to run one
unfiltered, unbounded ``read_aligned_table`` at ``blob_handling="all_binary"``, which materialises
EVERY row's bytes: serving one page cost the whole corpus, and the listing's ``limit`` was applied
only after the scan, so the listing's memory was bounded by the volume. A blob
column scanned at DEFAULT ``blob_handling`` yields ``struct<kind, position, size, blob_id,
blob_uri>`` and reads no object bytes at all, so ``size`` and ``has_payload`` are answerable for
free; the bytes are fetched for one row, by row id, only on the route that serves them. A PLAIN
``large_binary`` column has no descriptor, so there is nothing free to read: the listing leaves the
column out of its projection entirely and reports presence and length as unknown, because paying a
corpus read to fill in two fields is the exact cost this rung exists to refuse.

See ``docs/architecture/lance-blob-v2-findings.md`` for the measurements behind both rules.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Annotated

import lance
import pyarrow as pa
from fastapi import APIRouter, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from lance.blob import BlobFile
from pydantic import BaseModel, computed_field

from viewer.api.security import READS_DATASET, CallerDep
from viewer.api.v1.endpoints.media import MAX_BUFFERED_BLOB_BYTES, _handle_size, _stream_handle, served_media_type
from workbench.exceptions import NotFoundError, ServiceUnavailableError, UnauthorizedError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.absence import reads_as_absent
from workbench.lancekit.blobs import is_blob_field
from workbench.lancekit.catalog import bounded, translate_catalog_errors
from workbench.lancekit.predicate import eq
from workbench.media.deps import StateDep
from workbench.media.state import AppState
from workbench.sources import connect_as


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api", tags=["pages"])

#: The tabular columns a page row carries beside its blob. Kept explicit so a dataset that grows a
#: column does not silently start shipping it to the browser.
_PAGE_COLUMNS = ["id", "source_uri", "stage"]


class Page(BaseModel):
    """One page in a page dataset — metadata only; the bytes come from ``GET /api/page``."""

    id: int
    source_uri: str
    stage: str
    #: The payload's length in bytes, read from the blob DESCRIPTOR rather than from the bytes — the
    #: listing must never dereference a payload.
    #:
    #: TWO CASES WHERE THAT IS NOT A BYTE COUNT, stated rather than smuggled, and both report 0:
    #:
    #: * an EXTERNAL descriptor (``kind == EXTERNAL_KIND``), where ``size == 0`` means THE WHOLE
    #:   OBJECT, whose length the dataset does not carry (`lance_docs/lance/format/table/blob.md`). Resolving it would
    #:   mean a per-row HEAD against the object store.
    #: * a payload column that is plain ``large_binary`` rather than blob-v2, which carries no
    #:   descriptor at all. Resolving it would mean reading the payload of every listed row.
    #:
    #: Both resolutions are exactly the IO this listing exists to avoid, and no consumer branches on
    #: ``size`` (the lakehouse zone renders it as a caption), so an honest 0 beats a scan.
    size: int
    #: False when this row's payload is null. Surfaced rather than hidden: a row that failed to
    #: acquire is a real state of the dataset, and a viewer that silently skips it reports a corpus as
    #: complete when it is not.
    #:
    #: None means UNKNOWN, and only a payload column that is plain ``large_binary`` rather than
    #: blob-v2 produces it: that shape records presence nowhere but in the bytes, so the listing
    #: cannot answer without reading the corpus it is forbidden to read (`_page_rows` carries the
    #: measurement). None rather than an optimistic True because this is the field consumers BRANCH
    #: on — ``GET /api/page`` is the authority on presence for that shape, and a guess here would
    #: report a failed harvest as a servable page, which is the misreport `has_payload` exists to
    #: prevent.
    #:
    #: MODALITY-FREE. This was `has_image`, and the route it describes serves an ARBITRARY governed
    #: table — audio, video and PDF corpora included. CLAUDE.md's test for a shared seam is "would this
    #: be right for audio?", and `has_image` is not.
    has_payload: bool | None

    @computed_field
    @property
    def has_image(self) -> bool | None:
        """Deprecated alias for `has_payload`. REMOVE after one release.

        A rename is a wire change, and the web zones are their own Deployments — during a rolling
        upgrade an old `rask-web-lakehouse` pod talks to a new viewer, and `storage.ts` reads
        `has_image`. Dropping it in the same release would break exactly that window. `computed_field`
        rather than a plain property so it is actually SERIALISED; it mirrors rather than defaults, so
        the two can never disagree.
        """
        return self.has_payload


class PageListing(BaseModel):
    #: The catalog table id these pages came from (e.g. acme$media$pages).
    dataset: str
    pages: list[Page]


#: Magic-byte prefixes for the formats a governed corpus actually carries. Evidence, not assumption:
#: a JPEG really does start `FF D8 FF`.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)


def sniff_media_type(payload: bytes) -> str:
    """What these bytes are, or an honest admission that we cannot tell.

    The governed tier schema is `{id, payload, stage, lineage, source_rowid}` with `payload` OPAQUE,
    and `table` is a caller-supplied catalog id — so there is no MIME to read anywhere. This route used
    to answer `image/jpeg` regardless, which is false for the audio, video and PDF corpora the same
    route serves.

    `application/octet-stream` for anything unrecognised, because a guess is worse than an admission:
    "bytes I cannot describe" is true, while `image/jpeg` over a WAV is not.
    """
    for prefix, media_type in _MAGIC:
        if payload.startswith(prefix):
            return media_type
    # RIFF is a container: the format is the four bytes at offset 8, not the header.
    if payload.startswith(b"RIFF") and len(payload) >= 12:
        return {b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo", b"WEBP": "image/webp"}.get(payload[8:12], "application/octet-stream")
    return "application/octet-stream"


def open_page_table(state: AppState, table: str, caller: LakehouseCaller) -> lance.LanceDataset:
    """Open a table by its qualified name (``lake~acme$media$pages``), through its source's namespace as the caller.

    THROUGH THE CATALOG, never a caller-supplied URI. Taking an ``s3://`` parameter would let a caller point this
    endpoint at any object it names; resolving the id at the catalog keeps the viewer to the tables the catalog governs,
    read on the credential the catalog vends this caller (DescribeTable's storage options), so the viewer holds no store
    credential of its own.

    The caller's own token for the lakehouse rides the namespace client, never a service credential: the lakehouse decides
    the read for the person, and its 403 is final. Its refusals keep their meaning (`workbench.lancekit.catalog.translate_catalog_errors`): absent is 404, an unknown
    caller 401 (saying whether a bearer was sent at all), a refused one 403, and an unreachable or failing catalog 503, which a reader retries, never the terminal
    404. A store failure behind a table the catalog resolved is a 503 too, with the location kept out of the detail: it
    is a path the caller never supplied.
    """
    try:
        located = state.settings.sources.table_named(table)
    except ValueError as exc:
        raise NotFoundError(f"no table {table!r}: {exc}") from exc
    client = connect_as(located.source, caller)
    identifier = located.identifier
    try:
        with translate_catalog_errors():
            # No shared session: one shared across callers serves a later caller the store an earlier caller's live open
            # was vended (`workbench.lancekit.registry.CallerSessions`); this open lives for the request alone.
            return bounded(lambda: lance.dataset(namespace_client=client, table_id=identifier))
    except UnauthorizedError as exc:
        # "No credential sent" and "credential refused" send a reader in opposite directions, so the detail says which.
        detail = "no bearer was sent (the caller is anonymous)" if caller.token is None else "the caller's bearer was refused"
        raise UnauthorizedError(f"the catalog rejected the credential opening {table!r}: {detail}") from exc
    except (ValueError, OSError) as exc:
        if reads_as_absent(exc):
            raise NotFoundError(f"catalog does not know table {table!r}") from exc
        logger.exception("table %r is unreadable", table)
        raise ServiceUnavailableError(f"the storage backing {table!r} is unreadable right now") from exc


def _page_rows(ds: lance.LanceDataset, limit: int) -> tuple[pa.Table, bool]:
    """The listing's scan, bounded by the REQUEST, plus whether ``payload`` came back as descriptors.

    ``limit`` is pushed into the scan rather than applied to the result. That is the OOM half:
    capped at 500 by the query model but never handed to Lance, the read was bounded by the
    volume, so ~10k pages at ~1 MB was ~10 GB materialised to return 100 metadata rows.

    THE PAYLOAD COLUMN IS PROJECTED ONLY WHERE PROJECTING IT IS FREE. At DEFAULT ``blob_handling`` a
    blob-v2 ``payload`` arrives as ``struct<kind, position, size, blob_id, blob_uri>`` and no object
    bytes are read at all, so ``size`` and ``has_payload`` come for nothing. ``table`` is a free
    caller-supplied catalog id, though, so a ``payload`` that is a plain ``large_binary`` column is a
    reachable shape — and there the same scan returns the BYTES. Projecting it would hand a route
    gated at the metadata rung the payload of every listed row: 500 rows at ~1 MB is half a gigabyte
    materialised to answer a listing (the same OOM as the unbounded scan above). So that
    shape is scanned without the column, and `list_pages` reports what a metadata read can know.

    THE FIDELITY THAT COSTS IS FORCED, NOT CHOSEN — measured 2026-08-31 with ``/proc/self/io`` rchar
    over a 260 MB plain corpus on pylance 10.0.0, at ``data_storage_version`` 2.1 and 2.2 alike:
    projecting ``payload`` reads 260 MB, and so does every candidate that would recover presence or
    length more cheaply (``payload IS NOT NULL`` as a scan expression, a ``payload IS NULL`` filter,
    ``count_rows`` over that filter; ``octet_length`` is refused outright on a binary column).
    Dropping the column reads 10 KB. There is no third answer to buy back.

    The shape is asked of ``ds.schema``, not of the result: the scan strips the Arrow extension marker
    from the returned field (measured on pylance 10.0.0), so ``is_blob_field`` answers False for a blob
    column read back out of a table — and for the plain shape there is now no returned field to ask.
    """
    descriptors = is_blob_field(ds.schema.field("payload"))
    columns = [*_PAGE_COLUMNS, "payload"] if descriptors else list(_PAGE_COLUMNS)
    return ds.to_table(columns=columns, limit=limit), descriptors


def _take_page(ds: lance.LanceDataset, table: str, page_id: int) -> tuple[BlobFile | BytesIO, int, str]:
    """Resolve one page's ``id`` to an open payload handle, size and sniffed media type. All blocking IO.

    TWO SHAPES, decided by ``ds.schema`` exactly as `_page_rows` decides its listing: ``table`` is a
    free caller-supplied catalog id, so a ``payload`` that is a plain ``large_binary`` column rather
    than blob-v2 is a reachable shape. ``take_blobs`` refuses a non-blob column outright, so an
    unconditional take made the same table answer 200 on the listing and 500 here.

    Blob-v2: ``id`` -> stable ``_rowid`` -> ``take_blobs(ids=[...])``. ``ids`` survive deletes and
    compaction where positional ``indices`` do not, and no step pairs a blob read against a second
    scan, so the null-row landmine in this module's docstring cannot apply. The whole corpus is no
    longer read to find one row.

    Plain ``large_binary``: there is no take-path, so the filtered scan itself carries the bytes —
    still bounded by the request (one matched row, ``limit=1``), and the cell's own validity is the
    null signal. The bytes are wrapped in a ``BytesIO`` so both shapes hand the caller the same
    seekable handle contract.

    First match wins on both shapes, preserving what ``ids.index(page_id)`` did for a non-unique
    ``id`` column.

    Only 12 bytes are read to sniff: ``_MAGIC``'s longest prefix is 8 and the RIFF branch needs the
    four bytes at offset 8, so 12 is exact. The handle is rewound afterwards because the caller reads
    from the same one.

    Both 404s are decided HERE, before any response object exists — the rule
    ``services/viewer/tests/test_media_null_payload.py`` exists to enforce: once a streaming response
    has sent its headers the status is already chosen and an exception can no longer become one.
    """
    if not is_blob_field(ds.schema.field("payload")):
        rows = ds.to_table(columns=["payload"], filter=eq("id", page_id), limit=1)
        if rows.num_rows == 0:
            raise NotFoundError(f"no page with id {page_id} in {table!r}")
        cell = rows.column("payload")[0]
        if not cell.is_valid:
            raise NotFoundError(f"row {page_id} in {table!r} has no payload")
        payload = bytes(cell.as_py())
        return BytesIO(payload), len(payload), sniff_media_type(payload[:12])
    ids = ds.to_table(columns=["id"], filter=eq("id", page_id), with_row_id=True)
    if ids.num_rows == 0:
        raise NotFoundError(f"no page with id {page_id} in {table!r}")
    rowid = int(ids.column("_rowid")[0].as_py())
    blob = ds.take_blobs("payload", ids=[rowid])[0]
    if blob is None:
        # A registered page whose harvest produced no bytes. 404 with the reason, so the UI can say
        # "this page failed to harvest" instead of rendering a broken image icon. `None` in the slot
        # is how pylance 10.0.0 reports it; through 9.0.0 the row was omitted from the list entirely.
        raise NotFoundError(f"row {page_id} in {table!r} has no payload")
    size = _handle_size(blob)
    head = blob.read(min(12, size))
    blob.seek(0)
    return blob, size, sniff_media_type(head)


def _read_all(blob: BlobFile | BytesIO, size: int) -> bytes:
    """The whole payload, for a blob small enough to buffer."""
    with blob as f:
        return f.read(size)


@router.get("/pages", summary="Pages in a page dataset", dependencies=[READS_DATASET])
async def list_pages(
    state: StateDep,
    caller: CallerDep,
    table: Annotated[str, Query(description="Catalog table id, e.g. acme$media$pages.")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> PageListing:
    """Page metadata, in dataset order.

    Note what is NOT here: the image bytes. A listing that inlined them would move hundreds of
    megabytes to render a contact sheet; the browser fetches each page's bytes from ``/api/page``,
    which the ``<img>`` tag streams on demand.
    """
    ds = await run_in_threadpool(open_page_table, state, table, caller)
    rows, descriptors = await run_in_threadpool(_page_rows, ds, limit)
    pages: list[Page] = []
    for i in range(rows.num_rows):
        if descriptors:
            # Cell validity is the null signal for a descriptor column (correct from pylance 9;
            # lance-blob-v2-findings.md records that it is wrong only at 8.0.0, and every manifest
            # pins >= 10.0.0). No payload is dereferenced to answer either field.
            cell = rows.column("payload")[i]
            has_payload: bool | None = bool(cell.is_valid)
            size = int(cell.as_py()["size"]) if has_payload else 0
        else:
            # A plain `large_binary` payload was never scanned (`_page_rows`), so neither field is
            # answerable here at any price this rung may pay. The row itself is still listed: which
            # rows exist IS metadata, and dropping them would report the corpus as shorter than it is.
            has_payload = None
            size = 0
        pages.append(
            Page(
                id=rows.column("id")[i].as_py(),
                source_uri=rows.column("source_uri")[i].as_py() or "",
                stage=rows.column("stage")[i].as_py() or "",
                size=size,
                has_payload=has_payload,
            )
        )
    return PageListing(dataset=table, pages=pages)


@router.get("/page", summary="One row's payload bytes", dependencies=[READS_DATASET])
async def get_page(
    state: StateDep,
    caller: CallerDep,
    table: Annotated[str, Query(description="Catalog table id, e.g. acme$media$pages.")],
    page_id: Annotated[int, Query(alias="id", ge=0, description="The page's `id` column value.")],
) -> Response:
    """The raw image bytes for one page.

    Selected by the ``id`` COLUMN, never by row position: positional indexing into a blob read is
    the misattribution bug this module exists to avoid, and a caller holding an id from the listing
    must get that page or a 404 — never a different one. The filter pushes that selection into the
    scan, so the cost is one page rather than the corpus.

    Buffered while small, streamed above the threshold. ``payload`` is opaque and ``table`` is a free
    query parameter, so the response size is whatever the DATA names — the same reason `/api/media`
    draws this line, and the threshold is shared with it rather than re-derived. An EMPTY payload is
    deliberately a 200 with a zero-length body here, not the 404 `blob_response` gives a derivative:
    a governed row whose payload is genuinely zero bytes is present, and this route's absence signal
    is the null above.
    """
    ds = await run_in_threadpool(open_page_table, state, table, caller)
    blob, size, sniffed = await run_in_threadpool(_take_page, ds, table, page_id)
    media_type, safety = served_media_type(sniffed)
    # Read as the person, so cached for them alone (`media._DERIVATIVE_CACHE` says why).
    headers = {"Cache-Control": "private, max-age=300", "Vary": "Authorization", **safety}
    if size > MAX_BUFFERED_BLOB_BYTES:
        return StreamingResponse(
            _stream_handle(blob, size),
            media_type=media_type,
            # Known exactly from the probe, and safe to declare because the generator owns this
            # handle: the bytes it yields and the size measured here come from the same open blob.
            headers={**headers, "Content-Length": str(size)},
        )
    return Response(
        content=await run_in_threadpool(_read_all, blob, size),
        media_type=media_type,
        headers=headers,
    )
