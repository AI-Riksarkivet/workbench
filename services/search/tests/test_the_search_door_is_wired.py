"""The search service's door has to be connected to something: its real lifespan verifies the person.

`api/security.py` builds the dependencies and every route declares them, but they find their verifier on `app.state`,
which only the lifespan puts there; enabled and unwired, every route answers 503. So this runs the real app with its real
lifespan against the loopback provider, and requires the DPoP challenge a person without a token is owed, not a 503.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from search import main as main_mod
from search.core.config import SearchSettings, get_search_settings
from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, Provider


def test_a_request_without_a_token_is_challenged_not_503(monkeypatch: pytest.MonkeyPatch, oidc_provider: Provider) -> None:
    settings = SearchSettings.model_validate(
        {
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_OIDC_ISSUER": oidc_provider.issuer,
            "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
            "WORKBENCH_OIDC_IDP_ID": IDP_ID,
            "WORKBENCH_OIDC_ALLOW_INSECURE": True,
            "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
        }
    )
    monkeypatch.setattr(main_mod, "get_search_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_search_settings] = lambda: settings
    try:
        with TestClient(main_mod.app) as client:
            response = client.get("/api/search", params={"q": "x"})
    finally:
        main_mod.app.dependency_overrides.clear()

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"].startswith("DPoP ")
