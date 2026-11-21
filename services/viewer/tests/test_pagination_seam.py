"""One home for the page parameters, and a bound on how deep a caller may go.

The viewer's paged routes share one `PaginationParams` dependency and one `Page[Item]` envelope
(`viewer.api.v1.pagination`), with a `MAX_OFFSET` bound on how deep a caller may go. Offset only: the
viewer's paged routes are bounded listings, and a cursor or keyset sibling belongs beside it the day a
route needs one.

WHAT IS NOT IMPLEMENTED, and why, since the finding asks for it: `total = ds.count_rows() if page == 1
else None`. The finding's own verifier withdraws the cost model behind it — an unfiltered Lance
`count_rows()` is answered from fragment metadata, not a table scan, so it does not carry the
`SELECT COUNT(*)` cost the reference warns about, and the "pins a threadpool worker for a full scan"
claim is "asserted, not demonstrated". Making `total` optional on every page but the first would
degrade the envelope for an unproven saving, so it is not done.
"""

from __future__ import annotations

import pytest

from viewer.api.v1.pagination import MAX_OFFSET, PaginationParams, build_page, guard_offset
from workbench.exceptions import ValidationError


def test_the_offset_is_derived_not_restated() -> None:
    """Two copies of `(page - 1) * page_size` is how the two drift."""
    assert PaginationParams(page=3, page_size=24).offset == 48
    assert PaginationParams(page=1, page_size=24).offset == 0


def test_the_boundary_page_is_still_served() -> None:
    """An off-by-one here silently amputates the last legal page."""
    page_size = 100
    last_legal = MAX_OFFSET // page_size
    guard_offset(PaginationParams(page=last_legal, page_size=page_size))
    with pytest.raises(ValidationError):
        guard_offset(PaginationParams(page=last_legal + 2, page_size=page_size))


def test_the_envelope_computes_what_clients_should_not() -> None:
    """`pages`, `has_next`, `has_prev` come from the server — the reference's anti-pattern is a
    client doing this arithmetic against incomplete data."""
    page = build_page([1, 2, 3], total=7, params=PaginationParams(page=1, page_size=3))
    assert (page.total, page.pages, page.has_next, page.has_prev) == (7, 3, True, False)

    last = build_page([7], total=7, params=PaginationParams(page=3, page_size=3))
    assert (last.pages, last.has_next, last.has_prev) == (3, False, True)


def test_an_empty_result_does_not_claim_a_page() -> None:
    """Ceil division on zero is where this kind of helper usually reports `pages=0, has_next=True`."""
    empty = build_page([], total=0, params=PaginationParams(page=1, page_size=20))
    assert (empty.pages, empty.has_next, empty.has_prev) == (0, False, False)
