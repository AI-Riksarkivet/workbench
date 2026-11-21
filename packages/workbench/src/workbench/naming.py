"""The delimiter a lance-namespace id is joined with, and the shape a project id has."""

from __future__ import annotations

import re
from typing import Final


#: The delimiter that joins a table's or namespace's names into its string identifier (``acme$corpus``). The Lance
#: Namespace spec fixes it: a request naming no delimiter uses ``$`` (``ns_catalog/spec.yaml``, the ``delimiter``
#: parameter), so every id the workbench joins or splits uses this one.
CATALOG_DELIMITER: Final = "$"


#: The shape a project id must have: DNS-safe, lowercase alphanumeric plus hyphens, 3-63 characters, never starting or
#: ending with a hyphen, the rule a lakehouse mints its project ids by. A publish names a lakehouse namespace by it, so
#: the strictest of the names it becomes wins.
#:
#: **Matched with ``\Z``, not ``$``.** Python's ``$`` also matches just before a trailing newline, so ``"acme\n"`` would
#: satisfy a ``$``-anchored copy of this rule.
CONTROL_ID_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
CONTROL_ID_RE = re.compile(rf"^{CONTROL_ID_PATTERN}\Z")


def is_safe_project(value: object) -> bool:
    """True iff ``value`` is a project id of :data:`CONTROL_ID_PATTERN`'s shape, and so path-safe.

    A project id becomes part of a namespace id and an object path, so anything outside the rule (traversal dots,
    separators, whitespace) is refused at the boundary, never repaired.
    """
    return isinstance(value, str) and CONTROL_ID_RE.fullmatch(value) is not None
