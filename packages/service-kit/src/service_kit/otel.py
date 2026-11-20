"""OpenTelemetry wiring: opt-in OTLP/HTTP export of every service's traces, metrics and logs.

`make_service_app` calls :func:`setup_otel` for every app it builds, so each service exports as itself (its own
``service.name``) once ``WORKBENCH_OTEL_ENABLED`` is on, and nothing at all while it is off. The OTLP exporters read
``OTEL_EXPORTER_OTLP_*`` from the environment (endpoint, protocol, headers), so this module hardcodes no target. Where
the gateway asks who is sending, each export presents the token in ``WORKBENCH_OTEL_TOKEN_FILE`` as its bearer, read
when the export is sent: no telemetry credential is configured anywhere.

Where telemetry goes is one object, :class:`TelemetrySinks`: the OTLP exporters in a deployment, the SDK's in-memory
ones in a test. What :func:`setup_otel` installs it also removes (:meth:`Telemetry.close`, from the app's lifespan
teardown), since the SDK's providers, the library instrumentations and the root logger's handler are process-wide.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, override

import requests
from fastapi import FastAPI
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel, ConfigDict
from requests.auth import AuthBase

from service_kit.config import Settings


if TYPE_CHECKING:
    from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
    from opentelemetry.trace import Span


log = logging.getLogger(__name__)


#: Headers the request hook may copy onto a span, and nothing else.
#:
#: An ALLOWLIST rather than a denylist, because the reference's rule is absolute — "Don't put PII
#: (email, raw user id, auth tokens) in span attributes — those go to the trace backend forever" — and
#: a denylist is the wrong shape for a rule you cannot walk back. A header added upstream tomorrow is
#: excluded by default instead of leaking until someone notices.
_SPAN_HEADERS: dict[str, str] = {
    "x-request-id": "request.id",
}


def server_request_hook(span: Span | None, scope: Mapping[str, Any]) -> None:
    """Join the estate's own `X-Request-ID` to the span it belongs to.

    `RequestIDMiddleware` mints the id, stores it on `request.state`, echoes it to the caller and
    publishes it to the context var `setup_logging`'s filter reads — so the id a caller quotes from a
    failed request finds the log lines. This is the other half: the same id on the span, so the log
    line and the trace are one search.

    IN THE INSTRUMENTATION, not in another middleware. A hook buffers nothing, which is what makes it
    safe on a streamed body such as a 206 Range response — a `BaseHTTPMiddleware` layer
    fully buffers the response body, so a streaming route cannot carry one.

    Every app `make_service_app` builds carries it, since `setup_otel` passes it to `FastAPIInstrumentor.instrument_app`.

    THE PATH NEVER REACHES THE SPAN, only the route's template: a path can carry a credential (a signed media URL's
    token, `/api/signed-media/{token}`) and a query a person's search. The instrumentation records the path and query in
    its URL attributes before routing, so the hook empties each of them; the template the request matched is the span's
    name and its `http.route`, which the instrumentation sets itself.

    Defensive on both counts the reference's own example is: a missing header is normal (the hook runs
    on every request in every app), and a non-recording span must cost nothing.
    """
    if not (span and span.is_recording()):
        return
    present: Mapping[str, object] = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        if key in present:
            span.set_attribute(key, "")
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or []
    for raw_key, raw_value in headers:
        attribute = _SPAN_HEADERS.get(raw_key.decode("latin-1").lower())
        if attribute and raw_value:
            span.set_attribute(attribute, raw_value.decode("latin-1"))


#: The span attributes the HTTP server instrumentation fills from the request's URL, in either semantic convention.
_URL_ATTRIBUTES: Final = ("http.target", "http.url", "url.path", "url.query", "url.full")


#: Explicit bucket boundaries (milliseconds) for the HTTP duration histograms.
#:
#: [[XC-067]] THE DEFAULT LAYOUT MADE AN ALERT UNFIREABLE. OTel's default explicit boundaries stop at
#: 10,000 ms. Measured on the deployed estate 2026-09-21, `maintenance` had a mean request duration of
#: 39,504 ms and its histogram held 4 observations at `le=10000` against 37 at `le=+Inf` — 89% overflow.
#: `histogram_quantile` returns at most the highest FINITE bucket's upper bound, so every quantile
#: answered exactly 10000 and `HttpServerLatencyHigh` (`p95 > 15000`) returned zero series while the
#: service it exists to catch ran at 39.5 seconds.
#:
#: CHOSEN FROM MEASURED DURATIONS, not doubled arbitrarily. The same store at the same moment:
#: catalog ~34 ms, lineage ~370 ms, maintenance
#: ~39,500 ms. So the sub-100 ms resolution the fast services depend on is kept exactly as it was, and the
#: tail is extended past a sweep with room for one that degrades. Reaching further must not blind the
#: estate to its fast services, which is the obvious wrong fix.
HTTP_DURATION_BUCKETS_MS: Final[tuple[float, ...]] = (
    5, 10, 25, 50, 75, 100, 250, 500, 750, 1_000, 2_500, 5_000, 7_500, 10_000,
    15_000, 30_000, 60_000, 120_000, 300_000,
)  # fmt: skip


class _ProjectedToken(AuthBase):
    """The bearer an export presents: the token in the mounted file, read when the export is sent, so a token the kubelet
    replaced is the one presented. An export whose token cannot be read raises (`OSError`, or `ValueError` for an empty
    file) and is not sent: an anonymous export would only be refused."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @override
    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        token = self._path.read_text().strip()
        if not token:
            raise ValueError(f"the OTLP token file {self._path} is empty")
        r.headers["Authorization"] = f"Bearer {token}"
        return r


class TelemetrySinks(BaseModel):
    """Where one app's telemetry goes: a span processor, a metric reader and a log record processor."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    spans: SpanProcessor
    metrics: MetricReader
    logs: LogRecordProcessor

    @classmethod
    def otlp(cls, token_file: Path | None) -> TelemetrySinks:
        """The OTLP/HTTP exporters the environment configures, batched, each presenting ``token_file``'s token when one is
        named."""
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        def session() -> requests.Session | None:
            if token_file is None:
                return None
            sender = requests.Session()
            sender.auth = _ProjectedToken(token_file)
            return sender

        return cls(
            spans=BatchSpanProcessor(OTLPSpanExporter(session=session())),
            metrics=PeriodicExportingMetricReader(OTLPMetricExporter(session=session())),
            logs=BatchLogRecordProcessor(OTLPLogExporter(session=session())),
        )


#: How often the SDK's export path may report a failed export: one line per this, however many exports a refusing or
#: unreachable gateway fails (each signal's exporter reports every failed export, a line a second or more).
EXPORT_FAILURE_REPORT_S: Final = 60.0
#: The loggers the SDK reports a failed export on: each signal's OTLP/HTTP exporter and each batching processor
#: (opentelemetry-sdk and -exporter-otlp-proto-http 1.44.0).
_EXPORT_LOGGERS: Final = (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.exporter.otlp.proto.http._log_exporter",
    "opentelemetry.sdk._shared_internal",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.metrics._internal.export",
    "opentelemetry.sdk._logs._internal",
    "opentelemetry.sdk._logs._internal.export",
)


class _NotTheSdksOwn(logging.Filter):
    """Keeps the SDK's and the exporters' own records out of the OTLP log handler: a failed export's report, sent through
    the exporter that failed, fails again and is reported again, for as long as the gateway refuses."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


class _OncePerInterval(logging.Filter):
    """Lets one warning or worse through per ``seconds``, across every logger it is on; lesser records pass."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self._seconds = seconds
        self._next = 0.0
        self._lock = threading.Lock()

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                return False
            self._next = now + self._seconds
            return True


class Telemetry(BaseModel):
    """What :func:`setup_otel` installed for one app, so :meth:`close` can take it all down again."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    handler: logging.Handler
    reports: logging.Filter
    instrumented: list[Any]

    def close(self) -> None:
        """Remove the root logger's handler, undo the library instrumentations this app made, and shut the providers
        down, which sends what they still hold. Suppressed per step: a teardown that raises hides what comes after it."""
        logging.getLogger().removeHandler(self.handler)
        for instrumentor in reversed(self.instrumented):
            with suppress(Exception):
                instrumentor.uninstrument()
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            with suppress(Exception):
                provider.shutdown()
        # After the providers' last export, which a refusing gateway fails too.
        for name in _EXPORT_LOGGERS:
            logging.getLogger(name).removeFilter(self.reports)

    def close_within(self, seconds: float) -> bool:
        """:meth:`close`, waited for ``seconds`` at most: whether it finished. Sending what the providers hold waits on the
        gateway (10 s per signal by the SDK's default), and a process stopping must not; past ``seconds`` the rest is
        abandoned to the process's exit."""
        closing = threading.Thread(target=self.close, name="telemetry-close", daemon=True)
        closing.start()
        closing.join(seconds)
        return not closing.is_alive()


def _instrument(instrumentor: BaseInstrumentor, done: list[Any], **kwargs: object) -> None:
    """Instrument a process-wide library once, recording it so the app that did can undo it."""
    if instrumentor.is_instrumented_by_opentelemetry:
        return
    # An instrumentor whose library is absent or of another version declines (it logs why) and stays uninstrumented.
    instrumentor.instrument(**kwargs)
    if instrumentor.is_instrumented_by_opentelemetry:
        done.append(instrumentor)


def setup_otel(app: FastAPI, service_name: str, settings: Settings, sinks: TelemetrySinks | None = None) -> Telemetry | None:
    """Export ``app``'s traces, metrics and logs as ``service_name`` when ``settings.otel_enabled``; ``None`` when off.

    **`Settings` DECIDES, and the endpoint env never does.** `dagger call test` injects `OTEL_EXPORTER_OTLP_ENDPOINT` into
    every container for Dagger's own telemetry, and its collector rejects application metrics, so an ambient endpoint
    deciding would have every app a test builds retry against it with backoff. Off is off.

    ``sinks`` defaults to the OTLP exporters (:meth:`TelemetrySinks.otlp`). The providers are this app's own and are
    passed to every instrumentation, and the logs reach the provider through a handler of this app's own on the root
    logger: neither depends on the process-wide providers, which the SDK sets only once. Those are set too, for code that
    asks the API for a tracer or a meter. The ``workbench.audit`` records reach the logs pipeline on their own instrumentation
    scope, the logger's name (`service_kit.governed.audit`).
    """
    if not settings.otel_enabled:
        return None

    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk.metrics import Histogram
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource

    sinks = sinks if sinks is not None else TelemetrySinks.otlp(settings.otel_token_file)
    # The explicit `service.name` wins over `OTEL_SERVICE_NAME`; `OTEL_RESOURCE_ATTRIBUTES` adds the rest.
    resource = Resource.create({"service.name": service_name})

    # `shutdown_on_exit=False` on all three: the app's lifespan shuts them down within its bound
    # (`Telemetry.close_within`), and a shutdown the SDK registered at exit would wait on the gateway again, unbounded.
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    tracer_provider.add_span_processor(sinks.spans)
    # THE VIEW IS WHAT MAKES THE ALERT REACHABLE ([[XC-067]]). Registered for the server and client duration instruments
    # by name rather than by a wildcard, so a future instrument does not silently inherit a tail sized for a sweep.
    views = [
        View(instrument_type=Histogram, instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(HTTP_DURATION_BUCKETS_MS))
        for name in ("http.server.duration", "http.client.duration")
    ]
    meter_provider = MeterProvider(resource=resource, metric_readers=[sinks.metrics], views=views, shutdown_on_exit=False)
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger_provider.add_log_record_processor(sinks.logs)
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    set_logger_provider(logger_provider)

    # The logs reach THIS provider: the instrumentation's own handler would bind to the process-wide one, which the SDK
    # sets once per process.
    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(_NotTheSdksOwn())
    logging.getLogger().addHandler(handler)
    # A refused or unreachable gateway: one line per `EXPORT_FAILURE_REPORT_S`, not one per failed export.
    reports = _OncePerInterval(EXPORT_FAILURE_REPORT_S)
    for name in _EXPORT_LOGGERS:
        logging.getLogger(name).addFilter(reports)

    # One span per request: the instrumentation would otherwise add a span for every ASGI `receive` and `send`, and a
    # streamed 206 a video is sought through is sent in as many chunks as it holds.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        server_request_hook=server_request_hook,
        exclude_spans=["receive", "send"],
    )
    done: list[Any] = []
    # `inject_trace_context`, never `set_logging_format`: the latter calls `logging.basicConfig`, which does nothing beside
    # `setup_logging`'s handler; `setup_logging`'s own format prints the ids this attaches to every record.
    _instrument(LoggingInstrumentor(), done, tracer_provider=tracer_provider, inject_trace_context=True, enable_log_auto_instrumentation=False)
    _instrument(HTTPXClientInstrumentor(), done, tracer_provider=tracer_provider, meter_provider=meter_provider)
    # `requests`, aiohttp (the OpenFGA client's transport), botocore and urllib3 (the lance-namespace REST client's): an
    # uninstrumented expensive leg beside instrumented cheap ones makes a trace misleading, not just incomplete. Each is
    # guarded: a deployable without the library gets no client spans for it, never a service that cannot start.
    with suppress(ImportError):
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        _instrument(RequestsInstrumentor(), done, tracer_provider=tracer_provider, meter_provider=meter_provider)
    with suppress(ImportError):
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor

        _instrument(AioHttpClientInstrumentor(), done, tracer_provider=tracer_provider, meter_provider=meter_provider)
    with suppress(ImportError):
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        _instrument(BotocoreInstrumentor(), done, tracer_provider=tracer_provider)
    with suppress(ImportError):
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

        _instrument(URLLib3Instrumentor(), done, tracer_provider=tracer_provider, meter_provider=meter_provider)
    return Telemetry(
        tracer_provider=tracer_provider, meter_provider=meter_provider, logger_provider=logger_provider, handler=handler, reports=reports, instrumented=done
    )
