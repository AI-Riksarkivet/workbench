"""The RFC 9457 problem envelope for the sites that build a response rather than raise.

Its own module because ``workbench.body_limit`` is pure ASGI middleware outside the exception handlers and must answer
before the body is buffered, so it cannot raise; ``workbench.ns_errors`` and search's 429 build their answers from the
same envelope. A ``workbench.exceptions.DomainError`` is answered by ``workbench.exceptions``' own handler instead.
"""

from __future__ import annotations

from opentelemetry import trace


#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


def occurrence() -> dict[str, str]:
    """RFC 9457's optional ``instance`` for the error being answered: the W3C trace id of the request's span.

    A 5xx detail is redacted, so this id is what a caller gives an operator, and it is the trace the request already
    has, where the redacted error's log line sits. Empty when no span is active (tracing off), since no id would then
    find anything.
    """
    context = trace.get_current_span().get_span_context()
    return {"instance": format(context.trace_id, "032x")} if context.is_valid else {}


def problem_body(code: int, *, status: int, title: str, detail: str, slug: str | None = None) -> dict[str, object]:
    """The problem body for the sites that must BUILD a response rather than raise.

    The RFC 9457 members (``type``, ``title``, ``status``, ``detail``, and ``instance`` when a span is active), plus
    ``code``, the lance-namespace ``ErrorCode`` of the failure, so a lance-namespace error a source raised and a refusal
    the workbench made itself carry the same numeric kind, and ``error``, which repeats ``detail``. The ``type`` URIs
    share the base ``https://lance.org/problems/``, named for that error taxonomy. The SHAPE lives here, and the STATUS
    and HEADERS stay with the site that knows them.

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
