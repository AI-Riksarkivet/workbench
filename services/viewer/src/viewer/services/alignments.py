"""A page's word alignments, as the atlas and the chunk doors read them from a table's alignments column."""

from __future__ import annotations

import json
from typing import Any


def parse_alignments_json(raw: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Decode the alignments JSONB column to a Python list.

    Returns an empty list on null, missing, malformed, or non-list input — so
    the annotated return type holds for every branch (Lance may hand back either
    the raw JSON string or an already-decoded value).
    """
    if not raw:
        return []
    decoded = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return decoded if isinstance(decoded, list) else []
