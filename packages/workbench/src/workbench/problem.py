"""The RFC 9457 problem envelope every error body has, for the sites that build a response rather than raise.

Its own module because ``workbench.body_limit`` is pure ASGI middleware outside the exception handlers and must answer
before the body is buffered, so it cannot raise; ``workbench.ns_errors`` builds its answers from the same envelope.
"""

from __future__ import annotations

from opentelemetry import trace


#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


def occurrence() -> dict[str, str]:
    """The spec's optional ``instance`` for the error being answered: the W3C trace id of the request's span.

    ``instance`` "identifies the specific occurrence of the error", "a request or response ID, or anything that the
    implementation can recognize to trace" it (`lance_docs/ns_catalog/spec.yaml` ErrorResponse). A 5xx detail is
    redacted, so this id is what a caller gives an operator, and it is the trace the request already has, where the
    redacted error's log line sits. Empty when no span is active (tracing off), since no id would then find anything.
    """
    context = trace.get_current_span().get_span_context()
    return {"instance": format(context.trace_id, "032x")} if context.is_valid else {}


def problem_body(code: int, *, status: int, title: str, detail: str, slug: str | None = None) -> dict[str, object]:
    """The RFC 9457 + spec-0.9 envelope, for the sites that must BUILD a response rather than raise.

    Six keys, and the last two are not decoration: ``code`` is a REQUIRED, no-default field on the
    generated Lance-Namespace client's ``ErrorResponse`` model, so a client validating a four-key body
    RAISES rather than seeing a ``None``. The SHAPE lives here, and the STATUS and HEADERS stay with the site that
    knows them.

    ``detail`` doubles as the spec's ``error`` for the same reason ``problem_detail`` does it: one
    message, so a problem-details client and a spec client cannot be told two different things.

    ``slug`` overrides the ``type`` suffix for a site whose URI does not match its title: the 422 handler's title is
    "Validation Error" and its type ends in ``/validation``, since a slug derived from the title would put a space in
    the URI.
    """
    return {
        "type": f"https://lance.org/problems/{slug or title.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": int(code),
        "error": detail,
        **occurrence(),
    }
