"""The SDK's in-memory telemetry sinks, for a test that reads what an app exported.

Apart from `tests.media_services`, so a suite of the kit's own can use them without importing the services.
"""

from __future__ import annotations

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from workbench.otel import TelemetrySinks


class MemorySinks:
    """The SDK's in-memory exporters, synchronous, so what a request emitted is there when it returns."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.metrics = InMemoryMetricReader()
        self.sinks = TelemetrySinks(spans=SimpleSpanProcessor(self.spans), metrics=self.metrics, logs=SimpleLogRecordProcessor(self.logs))

    def finished_spans(self) -> tuple[ReadableSpan, ...]:
        return self.spans.get_finished_spans()

    def finished_logs(self) -> tuple[ReadableLogRecord, ...]:
        return self.logs.get_finished_logs()

    def metrics_data(self) -> MetricsData | None:
        return self.metrics.get_metrics_data()
