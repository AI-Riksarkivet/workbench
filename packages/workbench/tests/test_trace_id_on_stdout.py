"""Every stdout line carries its trace id and the diagnostics its caller recorded in `extra=`, and nothing else.

`setup_logging`'s handler formats with `DiagnosticFormatter`: the request id and the trace id have slots of their own,
fed by `CorrelationFilter` (so a record that never met OpenTelemetry still formats, `-` in each), an all-zero trace id
outside a span prints as `-`, and whatever the caller put in `extra=` follows the message. The fields a library stamps
on every record (`LoggingInstrumentor`'s `otel*`, Ray's `_ray_timestamp_ns`, uvicorn's `color_message`) are not
diagnostics and are left out.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from workbench.app import setup_logging


@pytest.fixture
def stdout_lines() -> Iterator[io.StringIO]:
    """The real `workbench-stdout` handler, redirected so the FORMATTED line can be read: what reaches an operator is
    the line, not the record."""
    root = logging.getLogger()
    previous = list(root.handlers)
    for handler in previous:
        root.removeHandler(handler)
    setup_logging("INFO")
    handler = next(h for h in root.handlers if h.get_name() == "workbench-stdout")
    # Narrowed, not asserted away: `setup_logging` builds a StreamHandler, and this fixture only works
    # because it is one — swapping the stream is how the FORMATTED line becomes readable.
    assert isinstance(handler, logging.StreamHandler)
    buffer = io.StringIO()
    original_stream = handler.stream
    handler.setStream(buffer)
    try:
        yield buffer
    finally:
        handler.setStream(original_stream)
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in previous:
            root.addHandler(h)


@pytest.fixture
def instrumented() -> Iterator[None]:
    """`LoggingInstrumentor` active, as `setup_otel` leaves it in a live service.

    Its trace-context injection only: the OTLP handler it would also attach binds to whatever logger provider the
    process holds, and after `test_otel.py` shut its provider down in this process that handler answers every record
    with a second line, `Shutdown called, ignoring Log.` (measured: `test_otel.py` then
    `test_a_record_carrying_nothing_extra_gains_no_trailing_noise` in one process failed it). These tests read the
    stdout line, which that handler never writes.
    """
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    instrumentor = LoggingInstrumentor()
    already = instrumentor.is_instrumented_by_opentelemetry
    if already:
        instrumentor.uninstrument()
    instrumentor.instrument(inject_trace_context=True, enable_log_auto_instrumentation=False)
    try:
        yield
    finally:
        instrumentor.uninstrument()


def test_a_record_written_inside_a_span_prints_its_trace_id(stdout_lines: io.StringIO, instrumented: None) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("unit") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        logging.getLogger("annotator.projects.saga").error("publish_step_failed")

    line = stdout_lines.getvalue()
    assert expected in line, f"the stdout line carries no trace id, so an operator holding one from a dashboard cannot grep for it: {line!r}"


def test_a_record_written_outside_a_span_prints_no_trace_id(stdout_lines: io.StringIO, instrumented: None) -> None:
    """Outside a span the instrumentor sets the id to zero; the line shows the placeholder instead."""
    logging.getLogger("catalog.api.v1.endpoints.tables").warning("no span here")

    line = stdout_lines.getvalue()
    assert "no span here" in line, f"the record never reached stdout: {line!r}"
    assert "0" * 32 not in line, f"an all-zero trace id was printed as though it were real: {line!r}"


def test_a_record_formats_when_otel_never_ran(stdout_lines: io.StringIO) -> None:
    """No `instrumented` fixture, so no `otelTraceID` on the record: the line is still written, with no logging error."""
    logging.getLogger("viewer.services.peaks").info("otel is off")

    line = stdout_lines.getvalue()
    assert "otel is off" in line, f"the record was lost when otel was disabled: {line!r}"
    assert "Traceback" not in line and "--- Logging error ---" not in line, f"the formatter raised on a record with no otel attributes: {line!r}"


def test_a_record_prints_the_diagnostics_it_carries_in_extra(stdout_lines: io.StringIO) -> None:
    """What a caller records in `extra=`, a string or not, follows the message on the line an operator reads."""
    logging.getLogger("search.services.service").warning(
        "result_cache_entry_dropped",
        extra={"reason": "graph write refused", "checked": 350},
    )

    line = stdout_lines.getvalue()
    assert "graph write refused" in line, f"the cause was recorded on the record and dropped by the formatter: {line!r}"
    assert "350" in line, f"a non-string diagnostic never reached the line: {line!r}"


def test_a_record_carrying_nothing_extra_gains_no_trailing_noise(stdout_lines: io.StringIO, instrumented: None) -> None:
    """`LoggingInstrumentor` stamps `otelTraceID`, `otelSpanID`, `otelServiceName` and `otelTraceSampled` on every
    record, as record attributes like an `extra`; none of them follows the message, since the correlation fields have
    their own slots."""
    logging.getLogger(f"{__name__}.bare").info("plain line")

    line = stdout_lines.getvalue().rstrip("\n")
    assert line.endswith("plain line"), f"a bare record grew a diagnostics tail: {line!r}"


def test_a_librarys_ambient_record_field_is_not_a_diagnostic(stdout_lines: io.StringIO) -> None:
    """Two libraries in the workbench stamp their own fields on records, and neither is a diagnostic.

    Importing Ray installs a log record factory that puts `_ray_timestamp_ns` on EVERY record in the process
    (`ray/_private/log.py:71-88`).

    uvicorn's `color_message` is the second: an ANSI-escaped copy of the same message, for uvicorn's own
    formatter to substitute (`uvicorn/logging.py:61`). Rendering it prints every startup line in the
    fleet twice, the second time full of escape codes.

    Logged to a logger this test owns rather than to `uvicorn.error`: uvicorn's shipped config gives the
    `uvicorn` tree `propagate: False` (`uvicorn/config.py:108`), so any test in the run that applies it
    detaches that name from the root handler and this assertion stops measuring the formatter at all.
    """
    # RAY'S OWN MECHANISM, not `extra=`. Ray installs a log record FACTORY that stamps the field on
    # every record (`ray/_private/log.py:71-88`), and passing the same key through `extra=` is both
    # unfaithful and order-dependent: once anything in the run has actually imported Ray, the factory
    # has already set the attribute and `logging` refuses the overwrite with
    # `KeyError: Attempt to overwrite '_ray_timestamp_ns' in LogRecord` (measured in the full suite).
    # Installing the factory reproduces production and is immune to whether Ray is imported.
    previous_factory = logging.getLogRecordFactory()

    def _ray_shaped_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        record.__dict__["_ray_timestamp_ns"] = 1789033301968524218
        return record

    logging.setLogRecordFactory(_ray_shaped_factory)
    try:
        logging.getLogger(f"{__name__}.ambient").info(
            "Started server process [%d]",
            4242,
            extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
        )
    finally:
        logging.setLogRecordFactory(previous_factory)

    line = stdout_lines.getvalue()
    assert "Started server process [4242]" in line, f"the record never reached stdout: {line!r}"
    assert "\x1b[36m" not in line, f"uvicorn's colour duplicate was rendered as a diagnostic: {line!r}"
    assert "1789033301968524218" not in line, f"Ray's ambient timestamp was rendered as a diagnostic: {line!r}"
