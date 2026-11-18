"""The search service's door has to be connected to something: its real lifespan verifies the person, and asks the
workbench's own OpenFGA who reads a source no catalog decides for.

`api/security.py` builds the dependencies and every route declares them, but they find their verifier and their store on
`app.state`, which only the lifespan puts there; enabled and unwired, every route answers 503. So this runs the real app
with its real lifespan against the loopback provider, and requires the DPoP challenge a person without a token is owed,
not a 503; and against a real OpenFGA, where a source's declared reader searches it and a signed-in stranger is refused,
and a store that appears only after the service started is taken up without a restart.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from search import main as main_mod
from search.core.config import SearchSettings, get_search_settings
from service_kit.governed import fga
from service_kit.governed.fga import user_object
from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, Provider
from tests.media_catalog import write_table
from tests.openfga import OpenFgaServer, OpenFgaStore


_DESCRIPTOR = {
    "identity": {"key_fields": ["doc_id"]},
    "display": {"body": "text"},
    "search": {"row_table": "chunks", "fts": {"table": "chunks", "column": "text"}},
}


def _search(settings: SearchSettings, monkeypatch: pytest.MonkeyPatch, *requests: tuple[dict[str, str], dict[str, str]]) -> list[httpx.Response]:
    """Each ``(params, headers)`` asked of the module-level app the pod serves, with its lifespan, on ``settings``."""
    monkeypatch.setattr(main_mod, "get_search_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_search_settings] = lambda: settings
    try:
        with TestClient(main_mod.app) as client:
            return [client.get("/api/search", params=params, headers=headers) for params, headers in requests]
    finally:
        main_mod.app.dependency_overrides.clear()


def test_a_request_without_a_token_is_challenged_not_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oidc_provider: Provider, openfga_store: OpenFgaStore
) -> None:
    settings = SearchSettings.model_validate(
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
    (response,) = _search(settings, monkeypatch, ({"q": "x"}, {}))

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"].startswith("DPoP ")


def test_a_source_no_catalog_decides_for_is_searched_by_its_declared_readers_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: OpenFgaStore
) -> None:
    """A lance-namespace source knows nobody, so the workbench's own OpenFGA decides who searches it (`reader` on
    `source:<name>`, from git): its declared reader is answered the hit, a signed-in stranger is refused, whether the
    search names the dataset, reads the deployment's default beside a ``table`` that names no dataset, or fans out."""
    dex = Provider("dex")
    root = tmp_path / "catalog"
    write_table(root, ["wh", "corpus", "chunks"], pa.table({"doc_id": ["d1"], "text": ["a ship sails"]}), descriptor=_DESCRIPTOR).create_scalar_index(
        "text", index_type="INVERTED"
    )
    settings = SearchSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "src", "impl": "dir", "properties": {"root": str(root)}, "readers": {"people": [f"{IDP_ID}~reader"]}}
            ],
            "MEDIA_DEFAULT_DATASET": "src~wh$corpus",
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
            "WORKBENCH_OIDC_ISSUER": dex.issuer,
            "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
            "WORKBENCH_OIDC_IDP_ID": IDP_ID,
            "WORKBENCH_OIDC_ALLOW_INSECURE": True,
            "WORKBENCH_DPOP_REQUIRED": False,
            "WORKBENCH_FGA_ENABLED": True,
            "WORKBENCH_FGA_API_URL": openfga_store.url,
            "WORKBENCH_FGA_STORE": openfga_store.name,
            "WORKBENCH_FGA_MODEL_ID": openfga_store.model_id,
        }
    )
    openfga_store.write((user_object(f"{IDP_ID}~reader"), "reader", "source:src"))
    queries = [
        {"q": "ship", "mode": "fts", "dataset": "src~wh$corpus"},
        {"q": "ship", "mode": "fts"},
        {"q": "ship", "mode": "fts", "table": "default"},
        {"q": "ship", "mode": "fts", "corpus": "src~wh$corpus", "table": "x"},
    ]
    try:
        answers = {
            sub: _search(settings, monkeypatch, *((query, {"Authorization": f"Bearer {dex.mint(sub)}"}) for query in queries)) for sub in ("reader", "stranger")
        }
    finally:
        dex.close()

    assert {sub: [answer.status_code for answer in got] for sub, got in answers.items()} == {"reader": [200] * 4, "stranger": [403] * 4}
    assert [hit["doc_id"] for hit in pa.ipc.open_stream(answers["reader"][0].content).read_all().to_pylist()] == ["d1"]


def test_a_store_that_appears_after_start_is_taken_up_without_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga: OpenFgaServer) -> None:
    """The service starts before its store holds the model (a model writer that ran late): every gated read answers 503,
    never allowed, and once the store is there the same running process answers the declared reader, with no restart."""
    monkeypatch.setattr(fga, "RESOLVE_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(fga, "RESOLVE_POLL_SECONDS", 0.1)
    dex = Provider("dex")
    root = tmp_path / "catalog"
    write_table(root, ["wh", "corpus", "chunks"], pa.table({"doc_id": ["d1"], "text": ["a ship sails"]}), descriptor=_DESCRIPTOR).create_scalar_index(
        "text", index_type="INVERTED"
    )
    name = f"late-{uuid.uuid4().hex}"
    settings = SearchSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "src", "impl": "dir", "properties": {"root": str(root)}, "readers": {"people": [f"{IDP_ID}~reader"]}}
            ],
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
            "WORKBENCH_OIDC_ISSUER": dex.issuer,
            "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
            "WORKBENCH_OIDC_IDP_ID": IDP_ID,
            "WORKBENCH_OIDC_ALLOW_INSECURE": True,
            "WORKBENCH_DPOP_REQUIRED": False,
            "WORKBENCH_FGA_ENABLED": True,
            "WORKBENCH_FGA_API_URL": openfga.url,
            "WORKBENCH_FGA_STORE": name,
        }
    )
    query = {"q": "ship", "mode": "fts", "dataset": "src~wh$corpus"}
    headers = {"Authorization": f"Bearer {dex.mint('reader')}"}
    monkeypatch.setattr(main_mod, "get_search_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_search_settings] = lambda: settings
    try:
        with TestClient(main_mod.app) as client:
            before = client.get("/api/search", params=query, headers=headers).status_code
            OpenFgaStore(openfga.url, name).write((user_object(f"{IDP_ID}~reader"), "reader", "source:src"))
            deadline = time.monotonic() + 10
            after = client.get("/api/search", params=query, headers=headers).status_code
            while after == 503 and time.monotonic() < deadline:
                time.sleep(0.1)
                after = client.get("/api/search", params=query, headers=headers).status_code
    finally:
        main_mod.app.dependency_overrides.clear()
        dex.close()

    assert (before, after) == (503, 200)
