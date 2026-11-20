"""The viewer, search and the annotator report ready only while they can serve, and stay live while a dependency is away.

`/readyz` answers from what each service's lifespan opened: its dataset registry, its sign-in verifier, and the
workbench's OpenFGA client, which a service keeps looking for while its store is not there (a model writer that has
not run yet). Until it is found every authorized door would answer 503, so the pod is out of rotation (503, `degraded`,
the component saying why), while `/livez` stays 200: a dependency's absence must not get the pod restarted. Once the
store is there the same pod reports ready, with no restart.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from service_kit.governed import fga
from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, Provider
from tests.media_services import SERVICES, running
from tests.openfga import OpenFgaServer, OpenFgaStore


@pytest.mark.parametrize("service", SERVICES)
def test_a_service_is_unready_but_live_until_its_store_is_found_then_ready(
    service: str, oidc_provider: Provider, openfga: OpenFgaServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fga, "RESOLVE_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr(fga, "RESOLVE_POLL_SECONDS", 0.05)
    store = f"test-{uuid.uuid4().hex}"
    env = {
        "WORKBENCH_SOURCES": [{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(tmp_path)}}],
        "WORKBENCH_FGA_ENABLED": True,
        "WORKBENCH_FGA_API_URL": openfga.url,
        "WORKBENCH_FGA_STORE": store,
        "WORKBENCH_OIDC_ENABLED": True,
        "WORKBENCH_OIDC_ISSUER": oidc_provider.issuer,
        "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
        "WORKBENCH_OIDC_IDP_ID": IDP_ID,
        "WORKBENCH_OIDC_ALLOW_INSECURE": True,
        "WORKBENCH_DPOP_REQUIRED": False,
        "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
    }
    with running(service, env, monkeypatch) as client:
        live, unready = client.get("/livez"), client.get("/readyz")
        OpenFgaStore(openfga.url, name=store)
        # Waits on the service's own look for the store, every 0.05 s here; 30 s only bounds a test that hangs.
        deadline = time.monotonic() + 30
        ready = client.get("/readyz")
        while ready.status_code != 200 and time.monotonic() < deadline:
            ready = client.get("/readyz")

    own = {"state": "unconfigured"} if service == "annotator" else {}
    assert (live.status_code, unready.status_code, unready.json()) == (
        200,
        503,
        {
            "status": "degraded",
            "components": {"registry": "ready", "sign_in": "ready", "authz": "store not found yet: every authorized request answers 503", **own},
        },
    )
    assert (ready.status_code, ready.json()) == (200, {"status": "ready", "components": {"registry": "ready", "sign_in": "ready", "authz": "ready", **own}})
