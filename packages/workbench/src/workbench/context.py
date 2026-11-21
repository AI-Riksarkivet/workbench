"""Request-scoped correlation, for code that does not take ``request`` as a parameter.

`RequestIDMiddleware` mints an id, keeps it on `request.state` and echoes `X-Request-ID` back, so a caller can quote
the id of a failed request. This publishes it where a log record, a repository method or a background helper reaches
it without `request` plumbed through every signature, and the stdout formatter prints it beside the trace id, so the
copy an operator reads first (`kubectl logs`) leads back to the request and its trace.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar


#: The current request's id. `"-"` rather than `""` so an absent value renders as a visible placeholder
#: in a log line instead of a blank that reads like a formatting bug.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    """The id for the request being served, or ``"-"`` outside one."""
    return request_id_ctx.get()


#: What `LoggingInstrumentor` writes when there is no active span — `"0"` before it has seen one, and
#: an all-zero id for an invalid span context. Neither is a trace anyone can look up, so both render
#: as the same visible placeholder rather than 32 zeros that read like a real id.
_ABSENT_TRACE_IDS = frozenset({"", "0", "0" * 32})


class CorrelationFilter(logging.Filter):
    """Stamp ``record.request_id`` and ``record.trace_id`` on every record.

    RENAMED from `RequestIdFilter` when it took on the second field: a class that stamps a trace id
    while calling itself the request-id filter is the stale naming this codebase keeps paying for.

    A FILTER, not a `LoggingMiddleware`, and the difference is the whole reason this reaches anything:
    a middleware can only annotate records it writes itself, while a filter on the root handler covers
    every module in every service — including libraries — through `setup_logging`'s single formatter,
    with no per-service edit.

    Never raises and never drops a record: a correlation aid that could swallow a log line would be a
    far worse bug than the missing id it fixes. That is also why `trace_id` is DERIVED here rather
    than named directly in the formatter — `logging` raises on a format field the record lacks, and a
    record with no `otelTraceID` is the normal case in dev, in tests, and in any service where
    `setup_otel` returned False. Guaranteeing the attribute is what lets the formatter reference it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        trace_id = str(getattr(record, "otelTraceID", "") or "")
        record.trace_id = "-" if trace_id in _ABSENT_TRACE_IDS else trace_id
        return True


# WHAT A RECORD CARRIES THAT NOBODY DELIBERATELY RECORDED. Derived from a real `LogRecord` rather than
# typed out, so a stdlib that grows a field does not start printing it as a diagnostic; `asctime` and
# `message` join it because the FORMATTER sets those, not `__init__`.
#
# The rest are attributes that ride every record from somewhere other than the caller:
# `LoggingInstrumentor` stamps the four `otel*` fields on all of them (`trace_id` above is already
# derived from one, and the format string prints it), the filter's own two have their own slots, and
# uvicorn's `color_message` is an ANSI copy of the message meant for uvicorn's formatter to substitute
# (`uvicorn/logging.py:61`) — rendering it prints every startup line twice.
#
# PRIVATE NAMES GO WITH THEM, and a library's behaviour is why rather than a principle: an engine SDK can
# install a log record factory that stamps a private nanosecond integer on EVERY record in the process
# (measured: `_ray_timestamp_ns`, `ray/_private/log.py:71-88`), so every process that imports it would have
# grown that field on every line. Naming it would fix this release and miss the next field it adds;
# leading-underscore is the convention a library reaches for when it means "mine, not the caller's", and
# no diagnostic in `services/` or `packages/` is named that way.
_AMBIENT_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "asctime",
    "message",
    "request_id",
    "trace_id",
    "otelTraceID",
    "otelSpanID",
    "otelServiceName",
    "otelTraceSampled",
    "color_message",
}


class DiagnosticFormatter(logging.Formatter):
    """Render the fields a caller passed in ``extra=`` after the message.

    A call site records a failure's cause in ``extra=`` and names only the event in the message, so a stdout line
    without the fields would read as a bare event with no cause, while the OTLP copy carries them.

    ``repr`` rather than ``str`` for the values: the commonest diagnostic is
    ``error=str(exc)``, and an unquoted exception string runs straight into the next field — or, when
    it spans lines, into what looks like a separate log record.

    NOTHING IS TRUNCATED: a cap would silently drop entries, and the formatter cannot know which call site can afford
    it. A call site whose diagnostic is unbounded bounds it there, where the meaning is known; the OTLP copy carries the
    whole value either way, so a cap here would only make the two tiers disagree.

    Secrets are the standing hazard of a tail like this, and the rule that keeps them out is the same
    one that governs `extra=` itself: a secret's NAME may be recorded, never its value.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        diagnostics = " ".join(f"{name}={value!r}" for name, value in vars(record).items() if not name.startswith("_") and name not in _AMBIENT_RECORD_FIELDS)
        return f"{line} {diagnostics}" if diagnostics else line
