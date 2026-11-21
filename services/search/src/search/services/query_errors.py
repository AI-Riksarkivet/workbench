"""Which Lance failure is the CALLER'S fault, and which is the service's.

A retrieval site that caught bare ``Exception`` and re-raised :class:`~workbench.exceptions.ValidationError` would
report an unreachable object store, an expired credential and a corrupt manifest as "you sent a bad request" (400):
the caller told to fix input that was fine, and the failure kept out of every 5xx-based alert.

The split is MEASURED against the pinned lancedb/pylance, not guessed. Every caller-input failure
the query path can produce — a predicate naming a missing column, a SQL parse error, a literal that
cannot be cast to the column's type, a wrong-dimension query vector, a ``select`` of a missing
column — arrives as ``RuntimeError`` (lancedb) or ``ValueError`` (pylance) whose message contains
``Invalid user input``. An outage does not. So the marker phrase is the whole classifier, and
:func:`is_caller_input_error` is deliberately the only thing in this module: a wider net (catching
``ValueError``, or matching "not found") would put the service's own faults back behind a 400.

The same message-classification shape `target.py` already uses for its open-table split, in one
place so the six call sites cannot drift apart.
"""

from __future__ import annotations


#: Lance/DataFusion's own prefix for "the query you handed me is malformed". Lowercased at the
#: comparison, because the two bindings differ in how they wrap it.
_CALLER_INPUT_MARKER = "invalid user input"


def is_caller_input_error(exc: BaseException) -> bool:
    """True when ``exc`` says the REQUEST was malformed, rather than that the service or its store is broken.

    A False answer means the exception must propagate: the global handler turns it into the 500
    that names an outage as an outage.
    """
    return _CALLER_INPUT_MARKER in str(exc).lower()
