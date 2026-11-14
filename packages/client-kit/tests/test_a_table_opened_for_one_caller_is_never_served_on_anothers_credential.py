"""A table opened for one caller is never served on another caller's credential, and each call carries its own trace.

The registry reads a dataset through a namespace client that carries the caller's bearer, and the catalog vends that
caller's store credential on DescribeTable. So what it caches (the handle, its client, its opens) is the caller's, and the
process-wide Lance and lancedb sessions must share no credential between callers. A second caller is answered on its own
bearer by the catalog and signs on its own vended key at the store, which refuses a key it did not admit.

Driven over real sockets, because pylance's REST and S3 clients are Rust: what the catalog and the store receive is
observable only at a server (``tests.vending_catalog``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from client_kit.lancekit.registry import DatasetRegistry
from client_kit.media.config import MediaSettings
from tests.media_catalog import write_table
from tests.vending_catalog import VendingCatalog, keyed_store, vend


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"}, "search": {"row_table": "chunks"}}
_ROWS = pa.table({"doc_id": ["a", "b"]}, schema=pa.schema([("doc_id", pa.string())], metadata={b"lance_media.descriptor": json.dumps(_DESCRIPTOR).encode()}))


def _registry(catalog: VendingCatalog, monkeypatch: pytest.MonkeyPatch) -> DatasetRegistry:
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    return DatasetRegistry(MediaSettings.model_validate({"MEDIA_CATALOG_URI": catalog.url}))


def test_a_second_caller_reads_on_its_own_bearer_and_its_own_vended_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with keyed_store({"KEY-OF-A"}, _ROWS) as (store, location):
        catalog = VendingCatalog(location, {"caller-a": vend(store.url, "KEY-OF-A"), "caller-b": vend(store.url, "KEY-OF-B")})
        registry = _registry(catalog, monkeypatch)
        assert registry.get("wh$ns", "caller-a").dataset("chunks").count_rows() == 2
        catalog.calls.clear()
        store.keys.clear()

        # The store refuses B's own key; served A's open, A's session or A's key, B would read the two rows.
        with pytest.raises((OSError, ValueError), match="403 Forbidden"):
            registry.get("wh$ns", "caller-b").dataset("chunks").count_rows()

        catalog.close()
    assert {bearer for _op, bearer, _trace in catalog.calls} == {"Bearer caller-b"}
    assert set(store.keys) == {"KEY-OF-B"}


def test_each_catalog_call_carries_the_trace_context_it_was_made_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog = VendingCatalog(write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS).uri, {"caller-a": {}})
    handle = _registry(catalog, monkeypatch).get("wh$ns", "caller-a")
    catalog.calls.clear()
    spans = [SpanContext(trace_id=trace_id, span_id=0x1, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED)) for trace_id in (0xA1, 0xB2)]

    for span in spans:
        with trace.use_span(NonRecordingSpan(span)):
            handle.has_table("chunks")

    catalog.close()
    assert [traceparent.split("-")[1] for _op, _bearer, traceparent in catalog.calls if traceparent] == [f"{0xA1:032x}", f"{0xB2:032x}"]


@pytest.fixture
def local_table(tmp_path: Path) -> Iterator[str]:
    yield write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS).uri


@pytest.mark.parametrize(
    ("expires_in_s", "described"),
    [
        pytest.param(900, 1, id="a-live-vend-is-reused"),
        pytest.param(30, 3, id="a-vend-near-expiry-is-not"),
    ],
)
def test_an_open_is_reused_until_its_vend_nears_expiry(monkeypatch: pytest.MonkeyPatch, local_table: str, expires_in_s: int, described: int) -> None:
    expires_at = str(int((time.time() + expires_in_s) * 1000))
    catalog = VendingCatalog(local_table, {"caller-a": {"expires_at_millis": expires_at}})
    handle = _registry(catalog, monkeypatch).get("wh$ns", "caller-a")
    catalog.calls.clear()

    for _ in range(3):
        handle.dataset("chunks")

    catalog.close()
    assert catalog.described() == described
