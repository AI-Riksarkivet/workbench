"""Arrow-IPC wire serving — the annotator's READ path.

The PixiJS engine consumes Arrow IPC directly (zero-copy geometry views), so we stream
the Lance annotations table as an Arrow IPC stream — the same wire format the atlas
``/points`` uses. A missing table degrades to an empty stream (a dataset with no
annotations yet is not an error).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, Response

from annotator.annotations.schema import ANNOTATIONS_TABLE, EMPTY_SCHEMA
from annotator.annotations.versions import checkout
from annotator.api.security import RawBearerToken
from annotator.corpus import resolve
from client_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from client_kit.media.deps import DatasetParam, StateDep
from service_kit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from service_kit.exceptions import NotFoundError


if TYPE_CHECKING:
    import pyarrow as pa


router = APIRouter(tags=["annotate"])


@router.get("/annotations/{doc_id}/{speech_id}/{chunk_id}")
def annotations(
    state: StateDep,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    caller_token: RawBearerToken = None,
    dataset: DatasetParam = None,
    version: Annotated[int | None, Query(ge=1, description="Read a historical version (time-travel).")] = None,
) -> Response:
    """Arrow IPC stream of the annotations for one media unit (doc + identity keys).

    The keys map positionally onto the descriptor's identity fields (same shape as
    the chunk / chunk-frame routes). ``version`` reads a HISTORICAL snapshot (Lance
    time-travel) for the compare-versions view; omitted ⇒ the latest."""
    corpus = resolve(state, dataset, caller_token)
    declared = corpus.declared
    doc_id = validate_doc_key(declared, doc_id)
    try:
        ds = corpus.handle.dataset(ANNOTATIONS_TABLE)
    except NotFoundError:
        # No annotations table yet — serve an empty stream so the client renders 0.
        return _empty_stream_response()
    where = chunk_key_filter(declared, doc_id, (speech_id, chunk_id))
    if version is not None:
        return _annotations_response(checkout(ds, version).to_table(filter=where), version)
    # The version the client echoes back on Save is the one this open is pinned at, so the rows served are that
    # version's: a commit landing after the open yields a spurious 409 on the next save (the client reloads), never a
    # header claiming a version whose rows were not served.
    return _annotations_response(ds.to_table(filter=where), int(ds.version))


def _empty_stream_response() -> Response:
    return Response(
        content=encode_arrow_stream(EMPTY_SCHEMA.empty_table()),
        media_type=ARROW_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Annotations-Version": "0"},
    )


def _annotations_response(table: pa.Table, version: int) -> Response:
    """The wire response: rows + the version the client echoes on Save."""
    return Response(
        content=encode_arrow_stream(table),
        media_type=ARROW_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Annotations-Version": str(version)},
    )
