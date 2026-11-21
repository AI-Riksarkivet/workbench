"""The viewer's offset pagination: params, a depth guard, and a page envelope.

Offset, because its paged routes are bounded listings (a dataset's rows, the system tables) a person pages through; a
feed or a deep walk would want a cursor or keyset, which belongs beside this the day a route needs one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from workbench.exceptions import ValidationError


#: How far into a listing a caller may ask to start. `Query(ge=1)` on a page number bounds NOTHING —
#: `?page=1000000` was legal and every store had to seek past ten million rows to answer it. The
#: reference's own figure; a route that genuinely needs to go deeper wants keyset, not a bigger number.
MAX_OFFSET = 10_000


class PaginationParams(BaseModel):
    """Page and size, with the offset DERIVED rather than restated at each call site."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def guard_offset(params: PaginationParams) -> PaginationParams:
    """Refuse a page whose offset exceeds :data:`MAX_OFFSET`, naming the way forward.

    A bare refusal is a dead end: the caller asked a legitimate question and needs to know which door
    answers it, so the message names keyset rather than only stating the limit.
    """
    if params.offset > MAX_OFFSET:
        raise ValidationError(
            f"page {params.page} starts at offset {params.offset}, beyond the {MAX_OFFSET} limit — use a keyset/cursor listing for deeper pages"
        )
    return params


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="per_page")] = 20,
) -> PaginationParams:
    """The guarded dependency: bounds the size in the SCHEMA and the depth at the door."""
    return guard_offset(PaginationParams(page=page, page_size=page_size))


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


class Page[Item](BaseModel):
    """The envelope, with the arithmetic done server-side.

    `pages`, `has_next` and `has_prev` are here because the reference's anti-pattern is a client
    computing them from incomplete data and drifting off-by-one from the server.
    """

    items: list[Item]
    total: int
    page: int
    page_size: int
    pages: int
    has_next: bool
    has_prev: bool


def build_page[Item](items: list[Item], total: int, params: PaginationParams) -> Page[Item]:
    pages = -(-total // params.page_size)  # ceil division
    return Page[Item](
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        pages=pages,
        has_next=params.page < pages,
        has_prev=params.page > 1,
    )
