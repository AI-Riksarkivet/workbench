"""A project id outside the rule a lakehouse mints its project ids by is refused (``workbench.naming.is_safe_project``).

A project id becomes part of a namespace id and an object path at publish, so the guard holds the strict rule
(lowercase, DNS-safe, 3-63 characters, no leading or trailing hyphen) and matches with ``fullmatch`` against a
``\\Z``-anchored pattern, since Python's ``$`` also matches before a trailing newline.
"""

from __future__ import annotations

import pytest

from workbench.naming import is_safe_project


#: Ids a looser rule (any case, underscores, short, a trailing hyphen) would admit.
_UNMINTABLE = [
    pytest.param("ACME", id="uppercase"),
    pytest.param("acme_bronze", id="underscore"),
    pytest.param("ab", id="two-chars-below-the-floor"),
    pytest.param("a" * 64, id="sixty-four-chars-over-the-ceiling"),
    pytest.param("acme-", id="trailing-hyphen"),
]


@pytest.mark.parametrize("value", _UNMINTABLE)
def test_an_id_the_catalog_could_not_mint_is_not_consumed(value: str) -> None:
    assert is_safe_project(value) is False


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("acme-bronze", id="hyphenated"),
        pytest.param("abc", id="three-chars-the-floor"),
        pytest.param("a" * 63, id="sixty-three-chars-the-ceiling"),
    ],
)
def test_an_id_of_the_mint_rule_is_accepted(value: str) -> None:
    assert is_safe_project(value) is True


def test_the_guard_does_not_lean_on_the_wire_anchor() -> None:
    """``$`` matches before a trailing newline in Python, and that newline would become part of an object path."""
    assert is_safe_project("acme\n") is False
