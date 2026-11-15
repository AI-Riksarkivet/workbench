"""Arrow-IPC wire serving — the annotator's READ path.

The PixiJS engine consumes Arrow IPC directly (zero-copy geometry views), so we stream
the Lance annotations table as an Arrow IPC stream — the same wire format the atlas
``/points`` uses. A missing table degrades to an empty stream (a dataset with no
annotations yet is not an error).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, Response

from annotator.annotations.access import MAY_READ_UNIT, UnitKey
from annotator.annotations.schema import EMPTY_SCHEMA
from annotator.annotations.unit import parse_unit
from annotator.annotations.versions import checkout
from annotator.api.security import AnnotationsDep, CallerDep
from annotator.corpus import resolve
from client_kit.media.deps import DatasetParam, StateDep
from service_kit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


if TYPE_CHECKING:
    import pyarrow as pa


router = APIRouter(tags=["annotate"])


@router.get("/annotations/{key:path}", dependencies=[MAY_READ_UNIT])
def annotations(
    caller: CallerDep,
    state: StateDep,
    annotations: AnnotationsDep,
    key: UnitKey,
    dataset: DatasetParam = None,
    version: Annotated[int | None, Query(ge=1, description="Read a historical version (time-travel).")] = None,
) -> Response:
    """Arrow IPC stream of the annotations for one unit of a task's item (`annotator.annotations.access`).

    The key's segments pair with the dataset's identity fields (`annotator.annotations.unit`). ``version`` reads a
    HISTORICAL snapshot (Lance time-travel) for the compare-versions view; omitted ⇒ the latest."""
    corpus = resolve(state, dataset, caller)
    unit = parse_unit(corpus.handle.descriptor, key)
    ds = annotations(corpus.id).open()
    if ds is None:
        # No annotations table yet — serve an empty stream so the client renders 0.
        return _empty_stream_response()
    where = unit.where
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
