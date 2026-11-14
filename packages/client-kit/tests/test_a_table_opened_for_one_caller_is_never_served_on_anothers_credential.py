"""A table opened for one person is never served on another person's credential, and each call carries its own trace.

The registry reads a dataset through a namespace client that presents the person's own token for the lakehouse, which
the service exchanged for them at the identity provider (RFC 8693), and the catalog vends that person's store credential
on DescribeTable. So what it caches (the handle, its client, its opens) is keyed on the person, and a token exchanged for
one person is never presented for another: a second person is answered on their own exchanged token by the catalog and
signs on their own vended key at the store, which refuses a key it did not admit.

Driven over real sockets, because pylance's REST and S3 clients are Rust: what the identity provider, the catalog and the
store receive is observable only at a server (``tests.identity``, ``tests.vending_catalog``).
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
from service_kit.governed.token_exchange import LakehouseCaller, LakehouseTokens
from tests.identity import CLIENT_ID, CLIENT_SECRET, LAKEHOUSE_AUDIENCE, BrowserKey, Provider
from tests.media_catalog import write_table
from tests.vending_catalog import VendingCatalog, keyed_store, vend


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"}, "search": {"row_table": "chunks"}}
_ROWS = pa.table({"doc_id": ["a", "b"]}, schema=pa.schema([("doc_id", pa.string())], metadata={b"lance_media.descriptor": json.dumps(_DESCRIPTOR).encode()}))


def _registry(catalog: VendingCatalog, monkeypatch: pytest.MonkeyPatch) -> DatasetRegistry:
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    return DatasetRegistry(MediaSettings.model_validate({"MEDIA_CATALOG_URI": catalog.url}))


def _caller(token: str) -> LakehouseCaller:
    return LakehouseCaller(subject=f"keycloak~{token}", token=lambda: token)


def test_a_token_exchanged_for_one_person_is_never_presented_for_another(monkeypatch: pytest.MonkeyPatch, oidc_provider: Provider, tmp_path: Path) -> None:
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    tokens = LakehouseTokens(issuer=oidc_provider.issuer, audience=LAKEHOUSE_AUDIENCE, client_id=CLIENT_ID, client_secret_file=str(secret))
    for person in ("a", "b"):
        tokens.remember(f"keycloak~{person}", oidc_provider.mint(person, jkt=BrowserKey().jkt))
    exchanged = {person: tokens.for_person(f"keycloak~{person}") for person in ("a", "b")}
    people = {person: LakehouseCaller(subject=f"keycloak~{person}", token=lambda p=person: tokens.for_person(f"keycloak~{p}")) for person in ("a", "b")}

    with keyed_store({"KEY-OF-A"}, _ROWS) as (store, location):
        catalog = VendingCatalog(location, {exchanged["a"]: vend(store.url, "KEY-OF-A"), exchanged["b"]: vend(store.url, "KEY-OF-B")})
        registry = _registry(catalog, monkeypatch)
        assert registry.get("wh$ns", people["a"]).dataset("chunks").count_rows() == 2
        catalog.calls.clear()
        store.keys.clear()

        # The store refuses B's own key; served A's open, A's session, A's token or A's key, B would read the two rows.
        with pytest.raises((OSError, ValueError), match="403 Forbidden"):
            registry.get("wh$ns", people["b"]).dataset("chunks").count_rows()

        catalog.close()
    assert {bearer for _op, bearer, _trace in catalog.calls} == {f"Bearer {exchanged['b']}"}
    assert set(store.keys) == {"KEY-OF-B"}
    # One exchange per person, each of that person's own workbench token, and none since: the cache answered the reads.
    assert [(r["grant_type"], r["subject_token"].count("."), r["audience"]) for r in oidc_provider.requests] == [
        ("urn:ietf:params:oauth:grant-type:token-exchange", 2, LAKEHOUSE_AUDIENCE)
    ] * 2


def test_each_catalog_call_carries_the_trace_context_it_was_made_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog = VendingCatalog(write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS).uri, {"caller-a": {}})
    handle = _registry(catalog, monkeypatch).get("wh$ns", _caller("caller-a"))
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
    handle = _registry(catalog, monkeypatch).get("wh$ns", _caller("caller-a"))
    catalog.calls.clear()

    for _ in range(3):
        handle.dataset("chunks")

    catalog.close()
    assert catalog.described() == described
