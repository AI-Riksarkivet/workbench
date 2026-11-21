"""Text the annotator keeps: what its store can write, checked where it writes.

PostgreSQL holds no NUL character in a `text` or a `jsonb` value, and a lone UTF-16 surrogate cannot be encoded as the
UTF-8 the database speaks. **The guarantee is at the store's write boundary:** every document the store writes (a
project, a task, a draft, a save's undo, the fields an unstarted task takes) is serialised once, by `storable_json`, and
that text is what is written; a NUL or a lone surrogate anywhere in it, in a key or a value at any depth, however it got
there (a field, or JSON a field held and a door decoded), is refused with `UnstorableDocument`, a 422 naming the
document. No decode can sit between that check and the write, because the checked text is the written one. A text
column the store writes (a person's kept name) passes `storable_text`.

`Text` is the same rule as a field's type, the early refusal that names the field: the annotator's request models and
string query parameters declare their strings with it, each with a length its meaning bounds, so Pydantic itself names
the offending field (`body.ontology.classes.0.name`, a mapping's key). A bound written on a `Text`
(`Annotated[Text, Field(max_length=...)]`) is checked after the content, and refused as "Value should have at most N
items after validation"; the request body's own cap bounds what either check reads.

Both run at C speed whatever the length: a substring or pattern search for the NUL, and one UTF-8 encode, which refuses a
lone surrogate. On a draft at its 8 MiB cap, `storable_json` takes 17 ms where a bare `json.dumps` takes 13 ms (measured
2026-11-18, aarch64), and it is the one serialisation the write makes.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from itertools import accumulate
from typing import Annotated, Any, Final

from pydantic import AfterValidator, Field

from workbench.exceptions import DomainError


#: The longest name, label, class, kind, state or principal a request names.
MAX_NAME: Final = 512
#: The longest id of a row, a shape, a link's end or a key a request names: one bound for every door that takes one, the
#: save, the send and the import alike (an import carries the ids another tool minted).
MAX_ID: Final = 4096
#: The longest text one annotation carries: a line's transcription, or the whole text of a text item's row.
MAX_TEXT: Final = 1024 * 1024
#: The longest mask one annotation carries: a base64 PNG data URL of the mask over its box (`ArrowDataPlugin`).
#: Measured: a 4096 x 4096 page mask with an anti-aliased region and 400 brushed blobs is 1.32 MB as a data URL, a
#: 1024 x 1024 one 0.25 MB. A draft holds at most 8 MiB in all (`state.store.MAX_DRAFT_BYTES`), so a mask past half of
#: it could not be kept beside the rest of an item's work.
MAX_MASK: Final = 4 * 1024 * 1024
#: The longest attributes object one annotation carries, as its JSON text: a request's `metadata` and an imported row's
#: `attributes` cell alike (`embedded_json`).
MAX_METADATA: Final = 64 * 1024
#: The deepest JSON `embedded_json` decodes. An attributes object is a flat map (depth 1, a nested value 2 or 3) and a
#: row's links an array of objects (2); the bound is checked before decoding, so the interpreter's own recursion limit,
#: which differs by thread stack and Python release, is never what refuses (`json.loads` raises `RecursionError` near
#: 10,000 levels, and 64 KiB holds 32,768).
MAX_JSON_DEPTH: Final = 32

#: A JSON string, escapes included, so the brackets inside one are not counted as nesting.
_JSON_STRING: Final = re.compile(r'"(?:[^"\\]|\\.)*"')
_BRACKET: Final = re.compile(r"[\[\]{}]")
_NESTING: Final = {"[": 1, "{": 1, "]": -1, "}": -1}

NUL: Final = "a NUL character (U+0000) cannot be stored"
SURROGATE: Final = "a lone surrogate (U+D800 to U+DFFF) is not text"
NOT_A_NUMBER: Final = "NaN and the infinities are not JSON numbers"

#: The escape `\u0000` after an even run of escaped backslashes (none included), the run matched from its start.
_ESCAPED_NUL: Final = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")


class UnstorableDocument(DomainError):
    """A document the store would write holds a NUL or a lone surrogate, which PostgreSQL cannot store."""

    status_code = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "Unprocessable Content"
    problem_type = "about:blank#unstorable-document"


def storable(value: str) -> str:
    """``value`` when the store can keep it; ``ValueError`` naming why not."""
    if "\x00" in value:
        raise ValueError(NUL)
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(SURROGATE) from None
    return value


#: A string the store can keep.
Text = Annotated[str, AfterValidator(storable)]
#: A name, label, class, kind, state or principal.
Name = Annotated[Text, Field(max_length=MAX_NAME)]
#: An id of a row, a shape, a link's end or a key.
Id = Annotated[Text, Field(max_length=MAX_ID)]


def storable_json(kind: str, document: Any) -> str:
    """The JSON text the store writes for ``document`` (compact, as it is stored), once it can hold it.

    Raises:
        UnstorableDocument: the document holds a NUL or a lone surrogate, in a key or a value at any depth, or a number
            JSON cannot spell (NaN, an infinity); ``kind`` names the document (`the task's draft`).
    """
    try:
        text = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise UnstorableDocument(f"{kind} cannot be kept: {NOT_A_NUMBER}") from None
    # `json.dumps` writes a NUL as the escape `\u0000` and a backslash as `\\`, so an escape is a NUL only when the
    # backslashes before it come in an odd run: `\\u0000` is a backslash followed by the text `u0000`.
    if "\\u0000" in text and _ESCAPED_NUL.search(text):
        raise UnstorableDocument(f"{kind} cannot be kept: {NUL}")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise UnstorableDocument(f"{kind} cannot be kept: {SURROGATE}") from None
    return text


def embedded_json(text: str, *, max_chars: int | None) -> Any:
    """The value of JSON text a request field or an imported cell carries: the annotator's one decoder of such text.

    ``max_chars`` is the longest text its carrier may hold (`MAX_METADATA` for attributes), or None where the request
    body's own cap and the draft's caps are the bound (an imported row's links).

    Raises:
        ValueError: ``text`` is longer than ``max_chars``, nests deeper than `MAX_JSON_DEPTH`, or is not JSON; the
            message reads after the name of what carried it ("attributes cell is not valid JSON (...)").
    """
    if max_chars is not None and len(text) > max_chars:
        raise ValueError(f"is longer than {max_chars} characters")
    brackets = _BRACKET.findall(_JSON_STRING.sub("", text))
    if brackets and max(accumulate(map(_NESTING.__getitem__, brackets))) > MAX_JSON_DEPTH:
        raise ValueError(f"nests deeper than {MAX_JSON_DEPTH} levels")
    try:
        return json.loads(text)
    except ValueError as exc:  # a JSONDecodeError, or an integer past Python's 4,300-digit conversion limit
        raise ValueError(f"is not valid JSON ({exc})") from exc


def storable_text(kind: str, value: str) -> str:
    """``value`` for a text column, once it can hold it; ``UnstorableDocument`` naming ``kind`` otherwise."""
    try:
        return storable(value)
    except ValueError as exc:
        raise UnstorableDocument(f"{kind} cannot be kept: {exc}") from None
