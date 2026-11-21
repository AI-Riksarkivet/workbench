"""Corpus rows on the wire as one Arrow IPC stream, for the doors that answer many rows of one kind.

A corpus row is not a fixed schema: its columns are whatever the dataset declares. So the schema is derived per
response, from the rows as their JSON form spells them (``TypeAdapter(...).dump_python(mode="json")``, what a JSON body
of the same rows would carry):

- a column whose non-null values are all strings, all numbers or all booleans is ``utf8``, ``float64`` or ``bool``;
- anything else (a nested object, a list, a column mixing kinds) is its JSON text, and its name is listed in the schema
  metadata under ``json_columns``, so a reader parses those back;
- a column with no non-null value is a null ``utf8`` column.

Numbers are ``float64`` whatever their JSON spelling, because a browser reads a JSON number as a double and an ``int64``
column would reach it as a ``BigInt``. A key absent from a row is that column's null in that row. The schema metadata
also carries ``rows``, the row count, which a table with no columns cannot otherwise say.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pyarrow as pa
from fastapi import Response
from pydantic import TypeAdapter

from workbench.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: Schema-metadata key naming the columns whose values are JSON text.
JSON_COLUMNS_KEY: Final = "json_columns"
#: Schema-metadata key carrying the row count.
ROWS_KEY: Final = "rows"

_ROWS: Final = TypeAdapter(list[dict[str, Any]])
_SCALARS: Final[dict[type, pa.DataType]] = {str: pa.string(), float: pa.float64(), bool: pa.bool_()}


def _kind(value: object) -> type | None:
    """The scalar kind a JSON-form value contributes to its column, ``None`` for a null, ``object`` for anything nested."""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool
    if isinstance(value, int | float):
        return float
    if isinstance(value, str):
        return str
    return object


def _column(values: list[object]) -> tuple[pa.Array, bool]:
    """One column's array, and whether it carries JSON text."""
    kinds = {kind for value in values if (kind := _kind(value)) is not None}
    if not kinds:
        return pa.array(values, type=pa.string()), False
    if len(kinds) == 1 and (kind := next(iter(kinds))) in _SCALARS:
        return pa.array(values, type=_SCALARS[kind]), False
    return pa.array([None if value is None else json.dumps(value) for value in values], type=pa.string()), True


def rows_to_arrow_stream(rows: list[dict[str, Any]]) -> bytes:
    """Encode ``rows`` as one Arrow IPC stream; columns are ordered by first appearance across every row."""
    wire = _ROWS.dump_python(rows, mode="json")
    names = list(dict.fromkeys(name for row in wire for name in row))
    arrays: list[pa.Array] = []
    json_columns: list[str] = []
    for name in names:
        array, is_json = _column([row.get(name) for row in wire])
        arrays.append(array)
        if is_json:
            json_columns.append(name)
    metadata = {ROWS_KEY: str(len(wire)), JSON_COLUMNS_KEY: json.dumps(json_columns)}
    schema = pa.schema([pa.field(name, array.type) for name, array in zip(names, arrays, strict=True)], metadata=metadata)
    return encode_arrow_stream(pa.Table.from_arrays(arrays, schema=schema))


def arrow_rows_response(rows: list[dict[str, Any]]) -> Response:
    """``rows`` as an ``application/vnd.apache.arrow.stream`` response."""
    return Response(content=rows_to_arrow_stream(rows), media_type=ARROW_STREAM_MEDIA_TYPE)
