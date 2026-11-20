"""A service's telemetry reaches the OTLP gateway as the service itself, on the pod's own token, and a gateway that
refuses or never answers costs the service little.

The factory wires the SDK's OTLP/HTTP exporters to the endpoint the environment names, and each export presents the token
in `WORKBENCH_OTEL_TOKEN_FILE` as its bearer, read when the export is sent: the kubelet replaces the file before the token
expires, so a token read once at start would be refused within the hour. A refused export is reported on a log line at
most once a minute and its report is never itself exported; a gateway that never answers holds the service's shutdown no
longer than `TELEMETRY_CLOSE_S`. The gateway here is a loopback server that records what arrives and answers as told.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from service_kit import app as service_app
from service_kit import make_service_app
from service_kit.config import Settings
from tests.telemetry_sinks import MemorySinks


class _Gateway(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str | None, bytes]]] = []
    #: The status to answer with, or None to never answer until `released` is set.
    status: ClassVar[int | None] = 200
    released: ClassVar[threading.Event] = threading.Event()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen.append((self.path, self.headers.get("Authorization"), body))
        status = type(self).status
        if status is None:
            # Held until the test ends, then answered, so the export waiting on it ends at once.
            type(self).released.wait()
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Gateway]]:
    _Gateway.seen, _Gateway.status, _Gateway.released = [], 200, threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The gateway is the only exporter configuration these tests run under. `dagger call test` sets a per-signal
    # endpoint and protocol in every container for its own telemetry (measured 2026-11-20: OTEL_EXPORTER_OTLP_TRACES_,
    # _METRICS_ and _LOGS_ENDPOINT and _PROTOCOL), and the SDK reads a per-signal variable before the general one.
    for name in [name for name in os.environ if name.startswith("OTEL_EXPORTER_OTLP_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}")
    yield _Gateway
    _Gateway.released.set()
    # A shutdown the bound abandoned finishes once its gateway answers: nothing of it outlives the test.
    for left in [t for t in threading.enumerate() if t.name == "telemetry-close"]:
        left.join(timeout=30)
    server.shutdown()
    thread.join(timeout=5)
    assert [t.name for t in threading.enumerate() if t.name == "telemetry-close"] == []


def _app(**env: object) -> FastAPI:
    router = APIRouter()

    @router.get("/thing")
    async def thing() -> dict[str, str]:
        logging.getLogger("svc-test").warning("a thing was asked for")
        return {"ok": "yes"}

    return make_service_app(title="svc-test", routers=[router], settings=Settings.model_validate({"WORKBENCH_OTEL_ENABLED": True, **env}))


def test_every_signal_is_exported_with_the_token_the_pod_holds_when_it_is_sent(gateway: type[_Gateway], tmp_path: Path) -> None:
    token = tmp_path / "otlp-token"
    token.write_text("first-projected-token\n")

    with TestClient(_app(WORKBENCH_OTEL_TOKEN_FILE=str(token))) as client:
        client.get("/api/v1/thing")
        token.write_text("rotated-projected-token\n")
    # The app's shutdown sent what the batches held.

    assert {path for path, _, _ in gateway.seen} == {"/v1/traces", "/v1/metrics", "/v1/logs"}
    assert {bearer for _, bearer, _ in gateway.seen} == {"Bearer rotated-projected-token"}


def test_the_providers_leave_their_shutdown_to_the_apps_lifespan_and_register_none_at_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK's providers register their own shutdown at exit unless told not to, and that one waits on the gateway
    without the lifespan's bound: a process whose lifespan gave up after `TELEMETRY_CLOSE_S` would wait again at exit."""
    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", lambda function, *args, **kwargs: registered.append(function) or function)

    app = _app()

    telemetry = app.state.telemetry
    providers = {id(provider) for provider in (telemetry.tracer_provider, telemetry.meter_provider, telemetry.logger_provider)}
    at_exit = [function for function in registered if id(getattr(function, "__self__", None)) in providers]
    telemetry.close()

    assert at_exit == []


def test_a_refusing_gateway_is_reported_once_a_minute_and_the_report_is_never_exported(gateway: type[_Gateway], caplog: pytest.LogCaptureFixture) -> None:
    gateway.status = 401
    caplog.set_level(logging.WARNING)
    app = _app()

    with TestClient(app) as client:
        for _ in range(3):
            client.get("/api/v1/thing")
            app.state.telemetry.logger_provider.force_flush()

    logs = [body for path, _, body in gateway.seen if path == "/v1/logs"]
    reports = [record for record in caplog.records if record.name.startswith("opentelemetry")]
    assert (len(logs) >= 3, [body for body in logs if b"Failed to export" in body], len(reports)) == (True, [], 1)


def test_a_streamed_range_response_is_one_server_span_measured_by_its_mounted_template() -> None:
    """A 206 a video is sought through is sent in many chunks; each would be a span of its own (the instrumentation's
    `send` spans), a trace a thousand spans long for one request. Its span and its duration metric name the same route
    template, with the prefix its router is mounted under."""
    router = APIRouter()

    @router.get("/clip")
    async def clip() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(8):
                yield b"x" * 1024

        return StreamingResponse(chunks(), status_code=206, headers={"Content-Range": "bytes 0-8191/65536"})

    sinks = MemorySinks()
    app = make_service_app(title="svc-test", routers=[router], settings=Settings.model_validate({"WORKBENCH_OTEL_ENABLED": True}), telemetry=sinks.sinks)
    with TestClient(app) as client:
        ranged = client.get("/api/v1/clip", headers={"Range": "bytes=0-8191"})
        metrics = sinks.metrics_data()

    targets = {
        point.attributes.get("http.target")
        for resource in (metrics.resource_metrics if metrics else [])
        for scoped in resource.scope_metrics
        for metric in scoped.metrics
        if metric.name == "http.server.duration"
        for point in metric.data.data_points
        if point.attributes is not None
    }
    spans = [(span.name, span.kind.name, (span.attributes or {}).get("http.route")) for span in sinks.finished_spans()]
    assert (ranged.status_code, spans, targets) == (206, [("GET /api/v1/clip", "SERVER", "/api/v1/clip")], {"/api/v1/clip"})


def test_a_gateway_that_never_answers_holds_the_shutdown_no_longer_than_its_bound(
    gateway: type[_Gateway], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.status = None
    monkeypatch.setattr(service_app, "TELEMETRY_CLOSE_S", 0.05)
    caplog.set_level(logging.WARNING, logger="service_kit.app")

    with TestClient(_app()) as client:
        client.get("/api/v1/thing")

    assert [(record.getMessage(), getattr(record, "waited_s", None)) for record in caplog.records if record.name == "service_kit.app"] == [
        ("telemetry_not_sent_at_shutdown", 0.05)
    ]
