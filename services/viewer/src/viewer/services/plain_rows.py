"""A table's rows as a person reads them, from its schema alone: the plain table every dataset can be shown as.

A dataset whose writer stamped no workbench descriptor still has a schema, and Lance answers paging, sorting, a
full-text query and a nearest-neighbour query on its own (pylance 13.0.0 ``LanceDataset.to_table``: ``offset``,
``limit``, ``order_by``, ``full_text_query``, ``nearest``). So the plain view guesses nothing a schema does not say:

- each column is rendered by its Arrow type (:func:`column_kind`): text, numbers, booleans, times, a vector as its
  dimension and its first values, a list or struct as JSON, binary and Lance blobs as their size;
- a column sorts only where Lance orders it (a primitive: numbers, text, booleans, times);
- text search is offered only on a column that carries an inverted index, and similar rows only on a vector column
  that carries a vector index (the table's own ``describe_indices``); nothing builds one;
- every read is bounded at the read (`read_page`, `read_row`): a page's depth, the keys a sort reads, the hits a search
  ranks, each cell's text and list length; a plain binary cell is never read (Lance reads one only whole), and a blob
  cell is sized and sniffed through Lance's blob API, a few bytes at a time, so an image or a recording can be
  previewed through the signed media path.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

import lance
import pyarrow as pa
from lance.dataset import ColumnOrdering
from pydantic import BaseModel, Field, JsonValue

from viewer.api.v1.endpoints.pages import sniff_media_type
from workbench.exceptions import NotFoundError, ValidationError
from workbench.lancekit.blobs import is_blob_field
from workbench.lancekit.introspect import TableInfo


#: The most rows one page answers.
MAX_PAGE: Final = 200
#: The most hits a text or similarity search ranks, which its pages are cut from.
MAX_HITS: Final = 1000
#: How many rows similar rows are ranked among at least, so a first page knows how many more there are.
SIMILAR_RANKED: Final = 100
#: How much of a text or JSON cell a page shows; the row's detail shows up to :data:`DETAIL_CHARS`.
PAGE_CHARS: Final = 240
DETAIL_CHARS: Final = 65536
#: How many items of a list a read takes.
LIST_ITEMS: Final = 50
#: The deepest a page may start: a page past it reads as many keys as it skips, and a person paging that far is better
#: served by a sort or a search.
MAX_OFFSET: Final = 100_000
#: How many of a vector's values a cell shows.
VECTOR_HEAD: Final = 6
#: How many leading bytes name a binary cell's media type (`pages.sniff_media_type`).
SNIFF_BYTES: Final = 12
#: The index kinds `workbench.lancekit.introspect` names (the type URL's tail, measured on pylance 13.0.0:
#: ``InvertedIndexDetails`` for a full-text index, ``VectorIndexDetails`` for every vector index).
FULL_TEXT_INDEX: Final = "Inverted"
VECTOR_INDEX: Final = "Vector"
#: Lance's own row id: stable across compaction where the table was created with stable row ids, else the row's address
#: at the version read, which is why a row is read back at the version its page named.
ROW_ID: Final = "_rowid"


class ColumnKind(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"
    DATE = "date"
    TIME = "time"
    DURATION = "duration"
    VECTOR = "vector"
    JSON = "json"
    BINARY = "binary"
    BLOB = "blob"
    OTHER = "other"


class ColumnView(BaseModel):
    """One column as the plain table shows it, and what can be done with it."""

    name: str
    arrow_type: str
    kind: ColumnKind
    sortable: bool
    #: A full-text index is on this column, so text search may name it.
    text_search: bool
    #: A vector index is on this column, so similar rows may be ranked by it.
    similar: bool
    dim: int | None = None


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class Row(BaseModel):
    rowid: int
    cells: dict[str, JsonValue]
    #: For a row's detail: the media type each binary or blob cell's bytes sniff as, where they sniff as anything.
    media: dict[str, str] = Field(default_factory=dict)
    #: For a search: the full-text score or the distance the row was ranked by.
    score: float | None = None


class RowsPage(BaseModel):
    #: The deepest offset a page may start at (:data:`MAX_OFFSET`).
    max_offset: int = MAX_OFFSET
    table: str
    #: The table's qualified name (``<source>~<namespace>$<table>``), which a signed media item names a cell's table by.
    qualified: str
    version: int
    columns: list[ColumnView]
    #: How many rows the page is cut from: the table's (or a search's hits, at most :data:`MAX_HITS`).
    total: int
    offset: int
    limit: int
    rows: list[Row]


class RowsQuery(BaseModel):
    """What one page asks: where, how many, in which order, or which search."""

    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)
    sort: str | None = None
    order: SortOrder = SortOrder.ASC
    #: Full-text query over ``column`` (or the first column with a full-text index).
    q: str | None = Field(default=None, min_length=1, max_length=500)
    column: str | None = None
    #: Rank by similarity to this row's value of the vector column ``vector``.
    near: int | None = Field(default=None, ge=0)
    vector: str | None = None
    #: The version the page reads; unset, the latest.
    version: int | None = Field(default=None, ge=1)


def column_kind(field: pa.Field) -> ColumnKind:
    """How a column is rendered, from its Arrow type alone."""
    kind = ColumnKind.OTHER
    t = field.type
    if is_blob_field(field):
        kind = ColumnKind.BLOB
    elif pa.types.is_fixed_size_list(t) and (pa.types.is_floating(t.value_type) or pa.types.is_integer(t.value_type)):
        kind = ColumnKind.VECTOR
    elif pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t):
        kind = ColumnKind.TEXT
    elif pa.types.is_boolean(t):
        kind = ColumnKind.BOOLEAN
    elif pa.types.is_integer(t):
        kind = ColumnKind.INTEGER
    elif pa.types.is_floating(t):
        kind = ColumnKind.FLOAT
    elif pa.types.is_decimal(t):
        kind = ColumnKind.DECIMAL
    elif pa.types.is_timestamp(t):
        kind = ColumnKind.TIMESTAMP
    elif pa.types.is_date(t):
        kind = ColumnKind.DATE
    elif pa.types.is_time(t):
        kind = ColumnKind.TIME
    elif pa.types.is_duration(t):
        kind = ColumnKind.DURATION
    elif pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t) or pa.types.is_binary_view(t):
        kind = ColumnKind.BINARY
    elif pa.types.is_nested(t) or pa.types.is_dictionary(t):
        kind = ColumnKind.JSON
    return kind


#: The kinds Lance orders by (a sort over these runs in the scan, ``order_by``).
_SORTABLE: Final = frozenset(
    {
        ColumnKind.TEXT,
        ColumnKind.INTEGER,
        ColumnKind.FLOAT,
        ColumnKind.DECIMAL,
        ColumnKind.BOOLEAN,
        ColumnKind.TIMESTAMP,
        ColumnKind.DATE,
        ColumnKind.TIME,
        ColumnKind.DURATION,
    }
)


def columns_of(schema: pa.Schema, info: TableInfo) -> list[ColumnView]:
    """The table's columns as the plain view shows them, with what the table's own indices allow on each."""
    indexed: dict[str, set[str]] = {}
    for index in info.indexes:
        for name in index.columns:
            indexed.setdefault(name, set()).add(index.index_type)
    views: list[ColumnView] = []
    for field in schema:
        kind = column_kind(field)
        kinds = indexed.get(field.name, set())
        views.append(
            ColumnView(
                name=field.name,
                arrow_type=str(field.type),
                kind=kind,
                sortable=kind in _SORTABLE,
                text_search=kind is ColumnKind.TEXT and FULL_TEXT_INDEX in kinds,
                similar=kind is ColumnKind.VECTOR and VECTOR_INDEX in kinds,
                dim=field.type.list_size if kind is ColumnKind.VECTOR else None,
            )
        )
    return views


def _clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else f"{text[:chars]}…"


def _plain(value: object) -> JsonValue:
    """A nested value as JSON can carry it: bytes as base64, times as ISO 8601, a non-finite float as text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return base64.b64encode(value[:64]).decode()
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def cell(kind: ColumnKind, value: object, *, chars: int) -> JsonValue:
    """One cell as the plain view renders it (`column_kind` decides how)."""
    if value is None:
        return None
    rendered: JsonValue
    match kind:
        case ColumnKind.TEXT:
            rendered = _clip(str(value), chars)
        case ColumnKind.INTEGER if isinstance(value, int) and abs(value) > 2**53:
            # Past 2^53 a JSON number loses digits in a browser, so the exact value travels as text.
            rendered = str(value)
        case ColumnKind.VECTOR if isinstance(value, list):
            rendered = {"dim": len(value), "head": [_plain(v) for v in value[:VECTOR_HEAD]]}
        case ColumnKind.JSON | ColumnKind.OTHER:
            rendered = _clip(json.dumps(_plain(value), ensure_ascii=False), chars)
        case ColumnKind.BINARY if isinstance(value, bytes):
            rendered = {"size": len(value)}
        case ColumnKind.BLOB if isinstance(value, dict):
            # A blob cell scanned at default handling is its descriptor (`lance_docs/lance/format/table/blob.md`).
            rendered = {"size": value.get("size")}
        case _:
            rendered = _plain(value)
    return rendered


def quote_column(name: str) -> str:
    """``name`` as pylance's SQL reads a column: in backticks (`lance_docs/lance/guide/read_and_write.md` § Filter
    push-down). The one quoting every plain door's SQL goes through.

    Raises:
        ValidationError: The name holds a backtick or a period, which pylance's SQL cannot name (a period it reads as a
            nested field, the same page's warning; a backtick ends the quote).
    """
    if "`" in name or "." in name:
        raise ValidationError(f"column {name!r} cannot be named in a filter")
    return f"`{name}`"


def _projection(columns: list[ColumnView], *, chars: int) -> dict[str, str]:
    """What a read takes of each column, bounded at the read rather than after it: text to ``chars`` (one more, so a
    clipped cell says so), a list to :data:`LIST_ITEMS` items, a blob as its descriptor, every other column whole, and a
    plain binary column not at all (Lance reads such a cell only whole: measured on pylance 13.0.0, its SQL has no length
    or prefix of a binary value). Columns are quoted with backticks, which pylance's SQL reads as identifiers (double
    quotes it reads as text, measured the same day); a name holding a backtick is read whole."""
    projection: dict[str, str] = {}
    for column in columns:
        if column.kind is ColumnKind.BINARY:
            continue
        quoted = f"`{column.name}`" if "`" not in column.name else None
        if quoted and column.kind is ColumnKind.TEXT:
            projection[column.name] = f"left({quoted}, {chars + 1})"
        elif quoted and column.kind is ColumnKind.JSON and column.arrow_type.startswith(("list<", "large_list<")):
            projection[column.name] = f"array_slice({quoted}, 1, {LIST_ITEMS})"
        else:
            projection[column.name] = quoted or column.name
    return projection


def _rows(records: list[dict[str, Any]], columns: list[ColumnView], *, chars: int) -> list[Row]:
    kinds = {c.name: c.kind for c in columns}
    rows: list[Row] = []
    for record in records:
        rows.append(
            Row(
                rowid=int(record[ROW_ID]),
                cells={name: cell(kind, record.get(name), chars=chars) for name, kind in kinds.items() if name in record},
            )
        )
    return rows


def _named(columns: list[ColumnView], name: str, what: str) -> ColumnView:
    found = next((c for c in columns if c.name == name), None)
    if found is None:
        raise ValidationError(f"the table has no column {name!r} to {what}")
    return found


def _by_rowid(ds: lance.LanceDataset, projection: dict[str, str], rowids: list[int]) -> list[dict[str, Any]]:
    """The rows ``rowids`` name, read through ``projection``, in that order (a row the table does not hold is left out)."""
    if not rowids:
        return []
    found = ds.to_table(columns=projection, filter=f"{ROW_ID} IN ({', '.join(str(int(r)) for r in rowids)})", with_row_id=True).to_pylist()
    at = {int(record[ROW_ID]): record for record in found}
    return [at[r] for r in rowids if r in at]


def at_version(ds: lance.LanceDataset, version: int | None) -> lance.LanceDataset:
    """``ds`` at ``version`` (its own when none is named).

    Raises:
        NotFoundError: The table holds no such version: pylance 13.0.0 raises an ``OSError`` saying the version's
            manifest "was not found" (measured 2026-11-16), which is the caller's miss, not the server's fault.
    """
    if version is None or version == int(ds.version):
        return ds
    try:
        return ds.checkout_version(version)
    except OSError as exc:
        if "not found" not in str(exc).lower():
            raise
        raise NotFoundError(f"the table holds no version {version}") from exc


def read_page(ds: lance.LanceDataset, info: TableInfo, qualified: str, query: RowsQuery) -> RowsPage:
    """One page of ``ds``: its rows in table order or sorted, a text search's hits, or the rows most like one row.

    Every read is bounded: a page starts no deeper than :data:`MAX_OFFSET`; a sort reads only its key column and row ids
    for the rows it skips, then the page's rows; a search or a ranking reads at most :data:`MAX_HITS` row ids and scores,
    then the page's rows; and each row is read through :func:`_projection`.

    Raises:
        ValidationError: The page asks for what the table cannot answer: a sort on a column Lance does not order, text
            search where no full-text index is, similar rows where no vector index is.
        NotFoundError: The row similar rows are asked for is not in the table.
    """
    columns = columns_of(ds.schema, info)
    projection = _projection(columns, chars=PAGE_CHARS)
    page = RowsPage(table=info.name, qualified=qualified, version=int(ds.version), columns=columns, total=0, offset=query.offset, limit=query.limit, rows=[])
    if query.q is not None:
        searchable = [c for c in columns if c.text_search]
        target = _named(columns, query.column, "search") if query.column else (searchable[0] if searchable else None)
        if target is None or not target.text_search:
            raise ValidationError("text search needs a full-text index, and this table has none on that column")
        ranked = ds.to_table(
            columns=["_score"],
            full_text_query={"query": query.q, "columns": [target.name]},
            limit=MAX_HITS,
            with_row_id=True,
            disable_scoring_autoprojection=True,
        )
        return _ranked(ds, page, projection, columns, ranked, "_score", query)
    if query.near is not None:
        vector = _named(columns, query.vector or "", "rank similar rows by") if query.vector else next((c for c in columns if c.similar), None)
        if vector is None or not vector.similar:
            raise ValidationError("similar rows need a vector index, and this table has none on that column")
        seed = _by_rowid(ds, {vector.name: f"`{vector.name}`"}, [query.near])
        if not seed or seed[0].get(vector.name) is None:
            raise NotFoundError(f"row {query.near} holds no {vector.name!r} to compare with")
        ranked = ds.to_table(
            columns=["_distance"],
            nearest={"column": vector.name, "q": seed[0][vector.name], "k": min(MAX_HITS, max(SIMILAR_RANKED, query.offset + query.limit) + 1)},
            with_row_id=True,
            disable_scoring_autoprojection=True,
        )
        ranked = ranked.take([i for i, rowid in enumerate(ranked.column(ROW_ID).to_pylist()) if rowid != query.near])
        return _ranked(ds, page, projection, columns, ranked, "_distance", query)
    page.total = ds.count_rows()
    if query.sort is None:
        found = ds.to_table(columns=projection, offset=query.offset, limit=query.limit, with_row_id=True).to_pylist()
    else:
        sort = _named(columns, query.sort, "sort by")
        if not sort.sortable:
            raise ValidationError(f"column {sort.name!r} holds {sort.kind} values, which Lance does not order")
        keys = ds.to_table(
            columns=[sort.name],
            offset=query.offset,
            limit=query.limit,
            order_by=[ColumnOrdering(sort.name, ascending=query.order is SortOrder.ASC)],
            with_row_id=True,
        )
        found = _by_rowid(ds, projection, [int(r) for r in keys.column(ROW_ID).to_pylist()])
    page.rows = _rows(found, columns, chars=PAGE_CHARS)
    return page


def page_of_rowids(ds: lance.LanceDataset, info: TableInfo, qualified: str, rowids: list[int], offset: int, limit: int) -> RowsPage:
    """One page of the rows ``rowids`` name, in that order: what a view's node lists, read through the plain rows' own
    projection."""
    columns = columns_of(ds.schema, info)
    page = RowsPage(table=info.name, qualified=qualified, version=int(ds.version), columns=columns, total=len(rowids), offset=offset, limit=limit, rows=[])
    found = _by_rowid(ds, _projection(columns, chars=PAGE_CHARS), rowids[offset : offset + limit])
    page.rows = _rows(found, columns, chars=PAGE_CHARS)
    return page


def _ranked(
    ds: lance.LanceDataset, page: RowsPage, projection: dict[str, str], columns: list[ColumnView], ranked: pa.Table, score: str, query: RowsQuery
) -> RowsPage:
    """A search's or a ranking's page: the hits' count, and the page's rows read by row id, each with its score."""
    page.total = ranked.num_rows
    window = ranked.slice(query.offset, query.limit)
    scores = dict(zip(window.column(ROW_ID).to_pylist(), window.column(score).to_pylist(), strict=True))
    found = _by_rowid(ds, projection, [int(r) for r in scores])
    page.rows = _rows(found, columns, chars=PAGE_CHARS)
    for row in page.rows:
        row.score = float(scores[row.rowid]) if scores.get(row.rowid) is not None else None
    return page


def read_row(ds: lance.LanceDataset, info: TableInfo, rowid: int) -> Row:
    """One row: every column through :func:`_projection` with text to :data:`DETAIL_CHARS`, and the media type of each
    blob cell whose first bytes name one, read through Lance's blob API as a size and a :data:`SNIFF_BYTES` range, never
    whole. A plain binary cell is neither sized nor previewed: Lance reads one only whole.

    Raises:
        NotFoundError: The table holds no such row at this version.
    """
    columns = columns_of(ds.schema, info)
    found = _by_rowid(ds, _projection(columns, chars=DETAIL_CHARS), [rowid])
    if not found:
        raise NotFoundError(f"no row {rowid} in table {info.name!r} at version {ds.version}")
    row = _rows(found, columns, chars=DETAIL_CHARS)[0]
    for column in columns:
        if column.kind is ColumnKind.BINARY:
            row.cells[column.name] = {"size": None}
        elif column.kind is ColumnKind.BLOB and row.cells.get(column.name) is not None:
            blob = ds.take_blobs(column.name, ids=[rowid])[0]
            if blob is None:
                continue
            try:
                media = sniff_media_type(blob.read_range(0, min(SNIFF_BYTES, blob.size())))
            finally:
                blob.close()
            if media != "application/octet-stream":
                row.media[column.name] = media
    return row


__all__ = [
    "MAX_OFFSET",
    "MAX_PAGE",
    "ROW_ID",
    "ColumnKind",
    "ColumnView",
    "Row",
    "RowsPage",
    "RowsQuery",
    "SortOrder",
    "at_version",
    "column_kind",
    "columns_of",
    "page_of_rowids",
    "quote_column",
    "read_page",
    "read_row",
]
