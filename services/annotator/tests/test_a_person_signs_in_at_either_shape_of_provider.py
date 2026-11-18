"""A person signs in at either shape of provider and is the principal the workbench's grants and the lakehouse's name.

The module-level app (`annotator.main.app`) with its own lifespan: sign-in on against a loopback provider of each shape
(`tests.identity.Provider`), the workbench's own store on the pinned OpenFGA (root `conftest.py::openfga_store`, found by
its name as a deployment finds it), and the projects in a PostgreSQL database of the test's own.

- `keycloak`: the token is bound to the browser's key and presented with its DPoP proof; the person is the issuer's idp
  id and their `sub`.
- `dex`: the provider binds no token (`WORKBENCH_DPOP_REQUIRED=false`), and its people are named by the connector map the
  lakehouse names them by (`WORKBENCH_OIDC_CONNECTORS`, rask's ADR 0208): the upstream user id Dex's `local` connector
  reports, under the idp id `dex`. The token is a plain bearer.

The organisation `acme` is administered by that principal in the store, so a person named any other way is in none.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from annotator.core.config import get_annotator_settings
from tests.identity import WORKBENCH_AUDIENCE, BrowserKey, Provider
from tests.openfga import OpenFgaStore
from tests.postgres import StateDatabase


#: How each shape names its person, and the principal that names.
SHAPES: dict[str, tuple[dict[str, str], str]] = {
    "keycloak": ({"WORKBENCH_OIDC_IDP_ID": "keycloak"}, "keycloak~ada"),
    "dex": (
        {"WORKBENCH_OIDC_IDP_ID": "dex", "WORKBENCH_OIDC_CONNECTORS": json.dumps({"local": "dex"}), "WORKBENCH_DPOP_REQUIRED": "false"},
        "dex~08a8684b-db88-4b73-90a9-3cd1661f5466",
    ),
}


@pytest.fixture(params=list(SHAPES))
def shape(request: pytest.FixtureRequest) -> Iterator[Provider]:
    name: Literal["keycloak", "dex"] = request.param
    running = Provider(name)
    yield running
    running.close()


@pytest.fixture
def client(
    shape: Provider, state_database: StateDatabase, openfga_store: OpenFgaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    naming, principal = SHAPES[shape.shape]
    env = state_database.env() | naming
    env |= {
        "WORKBENCH_SOURCES": json.dumps([{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(tmp_path)}}]),
        "WORKBENCH_OIDC_ENABLED": "true",
        "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": "false",
        "WORKBENCH_OIDC_ISSUER": shape.issuer,
        "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
        "WORKBENCH_OIDC_ALLOW_INSECURE": "true",
        "WORKBENCH_FGA_ENABLED": "true",
        "WORKBENCH_FGA_API_URL": openfga_store.url,
        "WORKBENCH_FGA_STORE": openfga_store.name,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    openfga_store.write((f"user:{principal}", "admin", "org:acme"))
    get_annotator_settings.cache_clear()
    from annotator.main import app

    try:
        with TestClient(app) as running:
            yield running
    finally:
        get_annotator_settings.cache_clear()


def test_a_person_signed_in_at_either_provider_is_the_principal_the_grants_name_and_creates_a_project(shape: Provider, client: TestClient) -> None:
    _naming, principal = SHAPES[shape.shape]
    browser = BrowserKey()
    if shape.shape == "keycloak":
        token = shape.mint("ada", jkt=browser.jkt)

        def headers(method: str, path: str) -> dict[str, str]:
            return {"Authorization": f"DPoP {token}", "DPoP": browser.proof(method, f"http://testserver{path}", token)}
    else:
        federated = {"connector_id": "local", "user_id": principal.removeprefix("dex~")}
        token = shape.mint("CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs", federated_claims=federated)

        def headers(method: str, path: str) -> dict[str, str]:
            del method, path
            return {"Authorization": f"Bearer {token}"}

    me = client.get("/me", headers=headers("GET", "/me"))
    created = client.post("/projects", json={"tenant": "acme", "slug": "charters"}, headers=headers("POST", "/projects"))

    assert me.status_code == 200, me.text
    assert (me.json()["sub"], me.json()["orgs"]) == (principal, [{"org": "acme", "role": "admin"}])
    assert created.status_code == 201, created.text
