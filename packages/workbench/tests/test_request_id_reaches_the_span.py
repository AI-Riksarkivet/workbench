"""The request id reaches the request's span, and no other header does.

`setup_otel` passes `server_request_hook` to `FastAPIInstrumentor.instrument_app`, so every app `make_service_app`
builds puts the `X-Request-ID` it echoes on its server span as `request.id`, the join between an id a caller quotes and
the trace. The hook runs inside the instrumentation and buffers nothing, so it is safe on a streamed body such as a 206
Range response. It reads that one header only: a credential such as `Authorization` never reaches a span attribute,
which the trace backend keeps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest


if TYPE_CHECKING:
    from opentelemetry.trace import Span


def test_the_hook_puts_the_request_id_on_the_span() -> None:
    """End to end against a real exporter: send the header, read the attribute off the span."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from workbench.otel import server_request_hook

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, server_request_hook=server_request_hook)
    try:
        TestClient(app).get("/thing", headers={"X-Request-ID": "abc123"})
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    attributes = [dict(span.attributes or {}) for span in exporter.get_finished_spans()]
    assert any(attrs.get("request.id") == "abc123" for attrs in attributes), f"no span carried request.id; got {attributes}"


def test_the_hook_survives_a_request_with_no_id() -> None:
    """A missing header must not raise — the hook runs on every request in every app."""
    from workbench.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": []})
    assert span.attrs == {}


def test_the_hook_is_a_no_op_on_a_non_recording_span() -> None:
    """A span that does not record, or no span, is left alone."""
    from workbench.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return False

        def set_attribute(self, key: str, value: object) -> None:  # pragma: no cover - must not run
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": [(b"x-request-id", b"abc")]})
    assert span.attrs == {}
    server_request_hook(None, {"type": "http", "headers": [(b"x-request-id", b"abc")]})


@pytest.mark.parametrize("header", ["authorization"])
def test_the_hook_reads_no_credential_header(header: str) -> None:
    """A credential header never reaches a span attribute."""
    from workbench.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": [(header.encode(), b"secret-value")]})
    assert "secret-value" not in str(span.attrs), f"the hook copied a {header} header onto the span"
