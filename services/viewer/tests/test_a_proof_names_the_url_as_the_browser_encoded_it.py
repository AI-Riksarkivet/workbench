"""A DPoP proof names the URL as the browser sent it, percent-encoding and all, and the viewer compares it so.

A dataset id carries the catalog's delimiter (`acme$letters`), which a browser sends percent-encoded
(`/api/datasets/acme%24letters/descriptor`) and signs that way in the proof's `htu` (RFC 9449 § 4.2). The service
compares `htu` with the URL the request arrived at after RFC 3986 § 6.2.2 normalisation (§ 4.3 rule 9), so it must read
the path as it came over the wire, not the percent-decoded path routing uses: decoded, every id with a reserved
character would answer 401. This drives the real viewer, with its real lifespan, against the loopback provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, BrowserKey, Provider
from tests.openfga import OpenFgaStore
from viewer import main as main_mod
from viewer.core.config import ViewerSettings, get_viewer_settings


@pytest.mark.parametrize(
    "signed",
    [
        pytest.param("http://testserver/api/datasets/acme%24letters/descriptor", id="as-sent"),
        pytest.param("http://testserver/api/datasets/acme%24l%65tters/descriptor", id="an-unreserved-letter-encoded"),
    ],
)
def test_a_proof_for_a_percent_encoded_path_is_accepted(
    signed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oidc_provider: Provider, openfga_store: OpenFgaStore
) -> None:
    settings = ViewerSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(tmp_path)}}],
            "WORKBENCH_FGA_ENABLED": True,
            "WORKBENCH_FGA_API_URL": openfga_store.url,
            "WORKBENCH_FGA_STORE": openfga_store.name,
            "WORKBENCH_FGA_MODEL_ID": openfga_store.model_id,
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_OIDC_ISSUER": oidc_provider.issuer,
            "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
            "WORKBENCH_OIDC_IDP_ID": IDP_ID,
            "WORKBENCH_OIDC_ALLOW_INSECURE": True,
            "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
        }
    )
    monkeypatch.setattr(main_mod, "get_viewer_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_viewer_settings] = lambda: settings
    browser = BrowserKey()
    token = oidc_provider.mint("ada", jkt=browser.jkt)
    try:
        with TestClient(main_mod.app) as client:
            response = client.get(
                "/api/datasets/acme%24letters/descriptor",
                headers={"Authorization": f"DPoP {token}", "DPoP": browser.proof("GET", signed, token)},
            )
    finally:
        main_mod.app.dependency_overrides.clear()

    # Past the door: the dataset names no declared source, which is the viewer's answer, not the verifier's.
    assert response.status_code == 404, response.text
