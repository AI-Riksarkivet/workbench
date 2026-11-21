"""One unit of a dataset, as an annotations door names it: a key path, read against the dataset's declared identity.

A unit is the row a task's item names (`ItemSource.keys`), written `<doc key>/<field>/…` with one segment per identity
field, in the descriptor's order (`Identity.key_fields`). The arity is the identity's, and each segment's type is its
column's in the dataset's row table (the default search's), so a key a dataset cannot have is refused in plain words
before any read or write, rather than answering a 404 for a path the route shape did not foresee or matching nothing.
"""

from __future__ import annotations

import re
from http import HTTPStatus

from pydantic import BaseModel

from workbench.exceptions import DomainError, ValidationError
from workbench.lancekit.descriptor import Declared
from workbench.lancekit.introspect import TableInfo
from workbench.lancekit.keys import validate_doc_key
from workbench.lancekit.predicate import and_, eq


#: A segment of an integer identity field.
_INTEGER = re.compile(r"^-?\d{1,19}$")


class UnitKeyError(DomainError):
    """A unit key this dataset's identity cannot name."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unprocessable Content"
    problem_type = "about:blank#unit-key"


class Unit(BaseModel):
    """A validated unit: its key as the client wrote it, and each identity field's typed value."""

    key: str
    values: dict[str, str | int]

    @property
    def where(self) -> str:
        """The predicate selecting this unit's rows of every task (values are escaped by `eq`)."""
        return and_(*(eq(field, value) for field, value in self.values.items()))

    def of_task(self, task: str) -> str:
        """The predicate selecting this unit's rows that are `task`'s work: a unit several tasks hold (a consensus item's
        replicas) keeps each one's rows apart."""
        return and_(self.where, eq("task_id", task))


def _column_type(declared: Declared, tables: dict[str, TableInfo], field: str) -> str | None:
    """The arrow type of an identity field: the default search's row table's, else any of the dataset's tables that
    carries the field. None when no table declares it."""
    search = declared.search
    candidates = [tables.get(search.row_table)] if search is not None else []
    candidates += list(tables.values())
    for table in candidates:
        column = table.column(field) if table is not None else None
        if column is not None:
            return column.arrow_type
    return None


def parse_unit(declared: Declared, tables: dict[str, TableInfo], key: str) -> Unit:
    """`key` read against the dataset's identity, or a `UnitKeyError` naming what does not fit."""
    identity = declared.identity
    segments = key.split("/")
    fields = [identity.doc_key, *(f for f in identity.key_fields if f != identity.doc_key)]
    if len(segments) != len(fields):
        raise UnitKeyError(f"unit key {key!r} has {len(segments)} part(s), and this dataset names a unit by {len(fields)}: {'/'.join(fields)}")
    try:
        doc = validate_doc_key(declared, segments[0])
    except ValidationError as exc:
        raise UnitKeyError(f"unit key {key!r}: {segments[0]!r} is not a {identity.doc_key}") from exc
    values: dict[str, str | int] = {identity.doc_key: doc}
    for field, segment in zip(fields[1:], segments[1:], strict=True):
        arrow_type = _column_type(declared, tables, field)
        if arrow_type is None:
            raise UnitKeyError(f"unit key {key!r}: this dataset declares no type for its identity field {field}")
        if arrow_type.startswith(("int", "uint")):
            if not _INTEGER.fullmatch(segment):
                raise UnitKeyError(f"unit key {key!r}: {field} is an integer, not {segment!r}")
            values[field] = int(segment)
        elif arrow_type in ("string", "large_string"):
            if not re.fullmatch(identity.doc_key_pattern, segment):
                raise UnitKeyError(f"unit key {key!r}: {segment!r} is not a {field}")
            values[field] = segment
        else:
            raise UnitKeyError(f"unit key {key!r}: {field} is {arrow_type}, which a unit key cannot name")
    return Unit(key=key, values=values)
