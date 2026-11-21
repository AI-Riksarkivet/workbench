"""The segments of a namespace or table id a source may be asked for.

An id reaches a catalog from a request (a dataset id, a table name, a signed media item), and a directory catalog maps a
name onto a path. pylance 13.0.0's directory catalog answered TableNotFound for ``../outside`` and ``a/../../outside``
(measured 2026-11-16 against a sibling ``outside.lance`` beside the root), and its REST client percent-encodes ``/``
and ``?`` inside a segment, but a name that could only mean a path is refused before any catalog sees it: an empty
segment, ``.`` or ``..``, a slash or backslash, NUL or another control character, or a delimiter an id is joined with.
"""

from __future__ import annotations

import re
from typing import Final


#: Separates a dataset id's source from its namespace (``lake~acme$corpus``).
SOURCE_SEPARATOR: Final = "~"
_UNSAFE: Final = re.compile(r"[/\\\x00-\x1f\x7f]")


def check_segments(identifier: list[str], delimiter: str) -> list[str]:
    """``identifier`` when every segment is one a catalog can only read as a name.

    Raises:
        ValueError: A segment is empty, ``.`` or ``..``, holds a path or control character, or a delimiter.
    """
    for segment in identifier:
        if segment in ("", ".", ".."):
            raise ValueError(f"id segment {segment!r} names no table or namespace")
        if _UNSAFE.search(segment):
            raise ValueError(f"id segment {segment!r} holds a path or control character")
        if delimiter in segment or SOURCE_SEPARATOR in segment or "$" in segment:
            raise ValueError(f"id segment {segment!r} holds a delimiter an id is joined with")
    return identifier


__all__ = ["SOURCE_SEPARATOR", "check_segments"]
