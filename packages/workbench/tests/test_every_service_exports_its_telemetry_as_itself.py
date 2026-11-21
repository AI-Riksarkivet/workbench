"""The viewer, search and the annotator each export their telemetry as themselves, and only when the deployment says so.

Each runs its real app and lifespan with sign-in on, against a loopback issuer and the workbench's own OpenFGA, and is
asked one of its doors with no token, which it refuses (401) and records on the audit trail. With observability on, the
request's server span, its duration metric and the audit record reach the sinks under the service's own `service.name`,
the audit record on its own instrumentation scope (`workbench.audit`), and nothing the request named (its path, its query)
rides a span or a record. With it off, nothing reaches them, though the environment names an OTLP endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, Provider
from tests.media_services import SERVICES, running
from tests.openfga import OpenFgaStore
from tests.telemetry_sinks import MemorySinks


#: A door each service refuses a caller without a token at, the marker in what the request names.
MARK = "marked-a91c"
DOORS = {"viewer": f"/api/datasets/archive~{MARK}/descriptor", "search": f"/api/search?q={MARK}&dataset=archive~x", "annotator": f"/me?probe={MARK}"}


def _env(provider: Provider, store: OpenFgaStore, root: Path, *, observed: bool) -> dict[str, object]:
    return {
        "WORKBENCH_SOURCES": [{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(root)}}],
        "WORKBENCH_FGA_ENABLED": True,
        "WORKBENCH_FGA_API_URL": store.url,
        "WORKBENCH_FGA_STORE": store.name,
        "WORKBENCH_FGA_MODEL_ID": store.model_id,
        "WORKBENCH_OIDC_ENABLED": True,
        "WORKBENCH_OIDC_ISSUER": provider.issuer,
        "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
        "WORKBENCH_OIDC_IDP_ID": IDP_ID,
        "WORKBENCH_OIDC_ALLOW_INSECURE": True,
        "WORKBENCH_DPOP_REQUIRED": False,
        "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
        "WORKBENCH_OTEL_ENABLED": observed,
    }


@pytest.mark.parametrize("service", SERVICES)
def test_a_refused_request_is_traced_measured_and_audited_as_the_service_that_refused_it(
    service: str, oidc_provider: Provider, openfga_store: OpenFgaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sinks = MemorySinks()
    with running(service, _env(oidc_provider, openfga_store, tmp_path, observed=True), monkeypatch, telemetry=sinks.sinks) as client:
        refused = client.get(DOORS[service])
        metrics = sinks.metrics_data()

    spans = sinks.finished_spans()
    server = [span for span in spans if span.kind.name == "SERVER"]
    audit = [record for record in sinks.finished_logs() if record.instrumentation_scope is not None and record.instrumentation_scope.name == "workbench.audit"]
    measured = {
        (scope.resource.attributes.get("service.name"), metric.name)
        for scope in (metrics.resource_metrics if metrics else [])
        for scoped in scope.scope_metrics
        for metric in scoped.metrics
    }
    # The test's own client is httpx in this process, and logs each request it makes, path and all, on `httpx`.
    services_records = [record for record in sinks.finished_logs() if record.instrumentation_scope is None or record.instrumentation_scope.name != "httpx"]
    named = [str(value) for span in spans for value in (span.attributes or {}).values()] + [
        str(value) for record in services_records for value in [record.log_record.body, *(record.log_record.attributes or {}).values()]
    ]
    assert refused.status_code == 401
    assert [(span.resource.attributes.get("service.name"), span.attributes and span.attributes.get("http.status_code")) for span in server] == [(service, 401)]
    assert [
        (
            record.resource.attributes.get("service.name"),
            (record.log_record.attributes or {}).get("audit.action"),
            (record.log_record.attributes or {}).get("audit.outcome"),
        )
        for record in audit
    ] == [(service, "authn", "failure")]
    assert (service, "http.server.duration") in measured, sorted(measured)
    assert [value for value in named if MARK in value] == []


@pytest.mark.parametrize("service", SERVICES)
def test_with_observability_off_a_service_exports_nothing(
    service: str, oidc_provider: Provider, openfga_store: OpenFgaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    sinks = MemorySinks()
    with running(service, _env(oidc_provider, openfga_store, tmp_path, observed=False), monkeypatch, telemetry=sinks.sinks) as client:
        refused = client.get(DOORS[service])

    assert (refused.status_code, sinks.finished_spans(), sinks.finished_logs()) == (401, (), ())
