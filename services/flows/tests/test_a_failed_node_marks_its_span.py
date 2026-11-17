"""A step's span names its node, and a failed node marks it.

The driver opens one span per step (`runner.drive_run`), and nothing else can carry what this service knows: in a run
of 256 steps no other span says WHICH node is slow or which kind fails. And a failed node RETURNS its failure rather than
raising it (a failed node is a normal outcome of a sandbox run that the builder paints red), so no `with` block sees an
exception and the span would stay UNSET: trace-based error search would show a clean estate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from flows.executor import run_node
from flows.models import FlowNode, NodeJob


SERVE = "http://serve.test"
#: The flows service's Serve credential, in a file as the projected ServiceAccount token is mounted.
TOKEN_FILE = str(Path(tempfile.mkdtemp()) / "serve-token")
Path(TOKEN_FILE).write_text("flows-sa-token")


def _job(node_id: str) -> NodeJob:
    return NodeJob(node=FlowNode(id=node_id, kind="model", config={"app": "htrflow"}), inputs=["seed"], serve_url=SERVE, serve_token_file=TOKEN_FILE)


async def _run_in_a_span(job: NodeJob) -> tuple[str, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with httpx.AsyncClient() as client:
        with provider.get_tracer("test").start_as_current_span("flows.node"):
            result = await run_node(job, client=client)
    return result.state.status, exporter


@pytest.mark.asyncio
@respx.mock
async def test_a_FAILING_node_marks_its_span_and_names_itself() -> None:
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(503, text="serve is down"))

    status, exporter = await _run_in_a_span(_job("ocr-1"))

    assert status == "failed", "the fixture did not produce a failed node"
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR, "a failed node leaves its span UNSET, so error search shows a clean estate"
    assert (span.attributes or {}).get("lance.flows.node_id") == "ocr-1", f"the span does not name the node: {dict(span.attributes or {})}"
    assert (span.attributes or {}).get("lance.flows.node_kind") == "model", "the span does not carry the node kind"


@pytest.mark.asyncio
@respx.mock
async def test_a_SUCCEEDING_node_names_itself_but_is_not_marked_an_error() -> None:
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text="Anno 1723"))

    status, exporter = await _run_in_a_span(_job("ocr-2"))

    assert status == "succeeded"
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is not StatusCode.ERROR, "a succeeding node must not be marked an error"
    assert (span.attributes or {}).get("lance.flows.node_id") == "ocr-2"
