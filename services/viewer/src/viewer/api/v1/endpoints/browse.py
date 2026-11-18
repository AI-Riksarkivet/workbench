"""A dataset's rows, a page at a time, for a dataset whose items ARE its rows.

A stamped dataset that declares no document (a corpus of texts: letters, notes, transcriptions with no picture or sound
behind them) has nothing the documents gallery (`GET /api/documents`) can list: each of its items is one row of its
declared row table. ``GET /api/rows`` lists those rows in the shape a search hit has (`atlas.hit_columns`), in the
table's own order at its current version, so the app browses them, opens one, and sends a page of them to a project
as it does search results.
"""

from typing import Any, Final

from fastapi import APIRouter, Response

from client_kit.lancekit.registry import table_dataset
from client_kit.media.deps import DatasetParam, StateDep
from client_kit.media.state import dataset_handle
from client_kit.pagination import Page, PaginationDep, build_page
from service_kit.exceptions import NotFoundError
from viewer.api.security import READS_DATASET, CallerDep
from viewer.api.v1.endpoints.atlas import finalize_hits, hit_columns


router = APIRouter(prefix="/api", tags=["browse"])

#: The most characters of one text cell a listing answers; the item's own door (`/api/atlas/chunks/by-key`) reads it whole.
LISTED_TEXT_CHARS: Final = 8192


@router.get("/rows", dependencies=[READS_DATASET])
def rows(caller: CallerDep, state: StateDep, pagination: PaginationDep, response: Response, dataset: DatasetParam = None) -> Page[dict[str, Any]]:
    """The declared row table's rows as hits, paged (the shared offset bounds, `client_kit.pagination`), each text cell
    cut at `LISTED_TEXT_CHARS` and a binary cell answered as null, each named on its row (`atlas.finalize_hits`). A
    person's own read, so kept by their browser alone (`private`) and revalidated on each use.

    404 for a dataset that declares no row table: there is nothing to list, and the app offers no browse of rows on it.
    """
    response.headers["Cache-Control"] = "private, no-cache"
    handle = dataset_handle(state, dataset, caller)
    declared = handle.declared
    if declared.search is None:
        raise NotFoundError(f"dataset {handle.id!r} declares no row table to list")
    ds = table_dataset(handle, declared.search.row_table)
    # An unfiltered count is answered from the fragments' metadata, not a scan (`system.documents`).
    total = ds.count_rows()
    table = ds.to_table(columns=hit_columns(declared, set(ds.schema.names)), limit=pagination.page_size, offset=pagination.offset)
    return build_page(finalize_hits(handle, table.to_pylist(), text_max=LISTED_TEXT_CHARS), total=total, params=pagination)
