"""A person's read reaches the lakehouse on the token their provider allows, and on no other.

The real viewer, its lifespan and its doors, against a loopback provider of each shape (`tests.identity.Provider`) and a
catalog stand-in that records the bearer each call carries and admits only a token addressed to the lakehouse, as the
lakehouse's doors do (`tests.vending_catalog`; the REST door takes the caller's token as `Authorization: Bearer`,
`lance_docs/ns_catalog/catalog/rest/index.md` § Identity Header Mapping):

- `keycloak`: the person's token is bound to the browser's key and addressed to the workbench alone, presented with its
  DPoP proof; the viewer exchanges it (RFC 8693) and the catalog sees only the exchanged token.
- `dex`: the provider binds no token and exchanges none of its own, and the sign-in client asked for the lakehouse's
  audience too; the person presents it as a bearer, and the catalog sees that very token, unchanged: the person's
  latest one, on a dataset handle the viewer keeps, and none past its own `exp`.

A token is exchanged only at the issuer that signed it: a further issuer's people are exchanged at their own issuer, or,
where it names no delegation, their read is refused and their token sent nowhere.
"""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Generator, Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import jwt
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from service_kit.governed.fga import user_object
from tests.identity import (
    CLIENT_ID,
    CLIENT_SECRET,
    IDP_ID,
    LAKEHOUSE_AUDIENCE,
    LAKEHOUSE_AUDIENCE_SCOPE,
    TOKEN_EXCHANGE,
    WORKBENCH_AUDIENCE,
    BrowserKey,
    Provider,
)
from tests.media_catalog import lakehouse_rest, write_table
from tests.openfga import OpenFgaStore
from tests.vending_catalog import VendingCatalog
from viewer import main as main_mod
from viewer.core.config import ViewerSettings, get_viewer_settings


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"}, "search": {"row_table": "chunks"}}
_ROWS = pa.table({"doc_id": ["a", "b"]})
URL = "http://testserver/api/datasets/lake~wh%24ns/descriptor"
#: A read that opens the row table on the dataset's cached handle, as every row read does.
CHUNKS = "/api/doc-chunks/a?dataset=lake~wh%24ns"


class _AdmitsTheLakehousesAudience(MutableMapping[str, Any]):
    """The catalog's answer per bearer: a local table's storage options to a token addressed to the lakehouse, and the
    spec's refusal to any other. ``vend_seconds`` makes each vend expire that soon, so the client describes the table
    again on its next open, as it does once a vend nears its expiry."""

    def __init__(self, vend_seconds: int | None = None) -> None:
        self._vend_seconds = vend_seconds

    def __getitem__(self, bearer: str) -> Any:
        aud = jwt.decode(bearer, options={"verify_signature": False}).get("aud")
        audiences = [aud] if isinstance(aud, str) else list(aud or [])
        if LAKEHOUSE_AUDIENCE not in audiences:
            return "refuse"
        return {} if self._vend_seconds is None else {"expires_at_millis": str(int((time.time() + self._vend_seconds) * 1000))}

    def __setitem__(self, bearer: str, answer: Any) -> None:
        raise TypeError("the answer is the token's audience, never set")

    def __delitem__(self, bearer: str) -> None:
        raise TypeError("the answer is the token's audience, never removed")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


@pytest.fixture(params=["keycloak", "dex"])
def provider(request: pytest.FixtureRequest) -> Iterator[Provider]:
    shape: Literal["keycloak", "dex"] = request.param
    running = Provider(shape)
    yield running
    running.close()


def _settings(provider: Provider, catalog: VendingCatalog, secret: Path, **over: object) -> ViewerSettings:
    shape = {
        "keycloak": {},
        "dex": {"WORKBENCH_DPOP_REQUIRED": False, "WORKBENCH_LAKEHOUSE_DELEGATION": "forward"},
    }[provider.shape]
    return ViewerSettings.model_validate(
        {
            **lakehouse_rest(catalog.url),
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
            "WORKBENCH_OIDC_ISSUER": provider.issuer,
            "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
            "WORKBENCH_OIDC_IDP_ID": IDP_ID,
            "WORKBENCH_OIDC_ALLOW_INSECURE": True,
            "WORKBENCH_LAKEHOUSE_AUDIENCE": LAKEHOUSE_AUDIENCE,
            "WORKBENCH_OIDC_CLIENT_ID": CLIENT_ID,
            "WORKBENCH_OIDC_CLIENT_SECRET_FILE": str(secret),
        }
        | shape
        | over
    )


@contextmanager
def _viewer(settings: ViewerSettings, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    """The module-level app the pod serves, with its lifespan, on ``settings``."""
    monkeypatch.setattr(main_mod, "get_viewer_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_viewer_settings] = lambda: settings
    try:
        with TestClient(main_mod.app) as client:
            yield client
    finally:
        main_mod.app.dependency_overrides.clear()


def _dex_token(dex: Provider, *, lifetime: int = 300) -> str:
    """A Dex-shaped person's token: addressed to the workbench and, by the sign-in's cross-client scope, the lakehouse."""
    return dex.mint("ada", audience=[WORKBENCH_AUDIENCE, LAKEHOUSE_AUDIENCE, "workbench-app"], azp="workbench-app", lifetime=lifetime)


def test_a_persons_read_reaches_the_lakehouse_exchanged_or_passed_on_as_the_provider_allows(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    catalog = VendingCatalog(table.uri, _AdmitsTheLakehousesAudience())
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    settings = _settings(provider, catalog, secret)
    monkeypatch.setattr(main_mod, "get_viewer_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_viewer_settings] = lambda: settings
    browser = BrowserKey()
    if provider.shape == "keycloak":
        token = provider.mint("ada", jkt=browser.jkt)
        headers = {"Authorization": f"DPoP {token}", "DPoP": browser.proof("GET", URL, token)}
    else:
        token = provider.mint("ada", audience=[WORKBENCH_AUDIENCE, LAKEHOUSE_AUDIENCE, "workbench-app"], azp="workbench-app")
        headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(main_mod.app) as client:
            response = client.get("/api/datasets/lake~wh%24ns/descriptor", headers=headers)
    finally:
        main_mod.app.dependency_overrides.clear()
        catalog.close()

    assert response.status_code == 200, response.text
    bearers = {bearer for _op, bearer, _trace in catalog.calls}
    exchanges = [request for request in provider.requests if request.get("grant_type") == TOKEN_EXCHANGE]
    if provider.shape == "keycloak":
        (presented,) = bearers
        assert presented is not None and presented != f"Bearer {token}"
        assert jwt.decode(presented.removeprefix("Bearer "), options={"verify_signature": False})["sub"] == "ada"
        assert [request["subject_token"] for request in exchanges] == [token]
    else:
        assert (bearers, exchanges) == ({f"Bearer {token}"}, [])


#: A token the deployment that passes people's tokens on must not pass: (the audiences it names, whether it is bound).
NEVER_PASSED_ON = {
    "one-not-naming-the-lakehouse": ([WORKBENCH_AUDIENCE, "workbench-app"], False),
    "one-bound-to-the-browsers-key": ([WORKBENCH_AUDIENCE, LAKEHOUSE_AUDIENCE], True),
}


@pytest.mark.parametrize(("audiences", "bound"), NEVER_PASSED_ON.values(), ids=NEVER_PASSED_ON.keys())
def test_a_token_passed_on_must_name_the_lakehouse_and_no_key_or_the_read_is_never_sent(
    audiences: list[str], bound: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dex = Provider("dex")
    table = write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    catalog = VendingCatalog(table.uri, _AdmitsTheLakehousesAudience())
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    settings = _settings(dex, catalog, secret)
    monkeypatch.setattr(main_mod, "get_viewer_settings", lambda: settings)
    main_mod.app.dependency_overrides[get_viewer_settings] = lambda: settings
    browser = BrowserKey()
    token = dex.mint("ada", audience=audiences, jkt=browser.jkt if bound else None)
    headers = {"Authorization": f"DPoP {token}", "DPoP": browser.proof("GET", URL, token)} if bound else {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(main_mod.app) as client:
            response = client.get("/api/datasets/lake~wh%24ns/descriptor", headers=headers)
    finally:
        main_mod.app.dependency_overrides.clear()
        catalog.close()
        dex.close()

    assert (response.status_code, catalog.calls) == (503, [])


def test_a_passed_on_read_presents_the_persons_latest_token_and_none_past_its_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dex = Provider("dex")
    table = write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    catalog = VendingCatalog(table.uri, _AdmitsTheLakehousesAudience(vend_seconds=30))
    # Two sign-ins' tokens: they differ in their lifetime, so they are two strings.
    first, renewed = _dex_token(dex, lifetime=300), _dex_token(dex, lifetime=600)
    # Past its own `exp` but inside the verifier's leeway, so the door admits it: nothing of the person's that has
    # expired is presented to the lakehouse.
    expired = _dex_token(dex, lifetime=-5)
    try:
        with _viewer(_settings(dex, catalog, tmp_path / "unused-secret"), monkeypatch) as client:
            on_first = client.get(CHUNKS, headers={"Authorization": f"Bearer {first}"})
            before_renewal = len(catalog.calls)
            on_renewed = client.get(CHUNKS, headers={"Authorization": f"Bearer {renewed}"})
            after_renewal = len(catalog.calls)
            on_expired = client.get(CHUNKS, headers={"Authorization": f"Bearer {expired}"})
    finally:
        catalog.close()
        dex.close()

    assert (on_first.status_code, on_renewed.status_code, on_expired.status_code) == (200, 200, 401), on_expired.text
    renewed_bearers = {bearer for _op, bearer, _trace in catalog.calls[before_renewal:after_renewal]}
    assert renewed_bearers == {f"Bearer {renewed}"}
    assert len(catalog.calls) == after_renewal


#: A token from a further issuer: (that issuer's `lakehouse` entry, the lakehouse source's identity, the status of its
#: person's read, whether that issuer exchanged it). A lakehouse read as the workbench needs no way to read as the person,
#: and is read by the readers the workbench's own OpenFGA names, the person among them.
FURTHER_ISSUER = {
    "names-no-delegation": (None, "person", 503, False),
    "exchanges-at-itself": ({"delegation": "exchange", "client_id": CLIENT_ID}, "person", 200, True),
    "names-no-delegation-read-as-the-workbench": (None, "service", 200, False),
}


@pytest.mark.parametrize(("lakehouse", "identity", "status", "exchanged"), FURTHER_ISSUER.values(), ids=FURTHER_ISSUER.keys())
def test_a_token_is_exchanged_only_at_the_issuer_that_signed_it(
    lakehouse: dict[str, str] | None,
    identity: str,
    status: int,
    exchanged: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    primary, further = Provider(), Provider()
    table = write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    catalog = VendingCatalog(table.uri, _AdmitsTheLakehousesAudience())
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    entry: dict[str, object] = {"issuer": further.issuer, "idp_id": "further", "allow_insecure": True}
    if lakehouse is not None:
        entry["lakehouse"] = lakehouse | {"client_secret_file": str(secret)}
    source: dict[str, object] = {"kind": "rask", "name": "lake", "uri": catalog.url, "identity": identity}
    store: dict[str, object] = {}
    if identity == "service":
        openfga_store: OpenFgaStore = request.getfixturevalue("openfga_store")
        openfga_store.write((user_object("further~ada"), "reader", "source:lake"))
        source["readers"] = {"people": ["further~ada"]}
        store = {
            "WORKBENCH_FGA_ENABLED": True,
            "WORKBENCH_FGA_API_URL": openfga_store.url,
            "WORKBENCH_FGA_STORE": openfga_store.name,
            "WORKBENCH_FGA_MODEL_ID": openfga_store.model_id,
        }
    settings = _settings(primary, catalog, secret, WORKBENCH_OIDC_PROVIDERS=[entry], WORKBENCH_SOURCES=[source], **store)
    browser = BrowserKey()
    token = further.mint("ada", jkt=browser.jkt)
    try:
        with _viewer(settings, monkeypatch) as client:
            response = client.get("/api/datasets/lake~wh%24ns/descriptor", headers={"Authorization": f"DPoP {token}", "DPoP": browser.proof("GET", URL, token)})
    finally:
        catalog.close()
        primary.close()
        further.close()

    assert response.status_code == status, response.text
    assert [request for request in primary.requests if token in request.values()] == []
    assert [request.get("subject_token") for request in further.requests] == ([token] if exchanged else [])


#: Each derived read of a set-up, with picks the gate test's one-row table answers.
DERIVED_READS: list[tuple[str, dict[str, str]]] = [
    ("profile", {}),
    ("atlas", {"vector": "v"}),
    ("graph", {"shape": "self", "source": "doc_id", "target": "parent"}),
    ("graph/check", {"shape": "self", "source": "doc_id", "target": "parent"}),
    ("graph/rows", {"shape": "self", "source": "doc_id", "target": "parent", "key": "s:a"}),
    ("tree", {"shape": "path", "node": "doc_id"}),
    ("tree/check", {"shape": "path", "node": "doc_id"}),
    ("tree/rows", {"shape": "path", "node": "doc_id", "key": '{"path":["a"]}'}),
]


@pytest.mark.parametrize("kind", ["namespace", "lakehouse-read-as-the-workbench"])
def test_a_source_no_catalog_decides_for_is_read_by_its_declared_readers_alone(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: OpenFgaStore
) -> None:
    """A lance-namespace source knows nobody, and a lakehouse read as the workbench sees only the workbench: the
    workbench's own OpenFGA decides who reads either (`reader` on `source:<name>`, from git). A declared reader lists,
    opens, reads its rows and has a cell signed; a signed-in stranger sees the source omitted, is refused the dataset, its
    rows and the signing;
    and a link signed for the reader stops serving once they are no longer a reader. A set-up's derived reads (a column
    profile, the atlas, the graph, the tree) stand behind the same gate."""
    dex = Provider("dex")
    rows = pa.table(
        {
            "doc_id": ["a"],
            "parent": ["a"],
            "payload": [b"\x89PNG\r\n\x1a\n...."],
            "v": pa.FixedSizeListArray.from_arrays(pa.array([0.5, 1.5], pa.float32()), 2),
        }
    )
    table = write_table(tmp_path / "catalog", ["wh", "ns", "chunks"], rows)
    catalog = VendingCatalog(table.uri, _AdmitsTheLakehousesAudience())
    readers = {"people": [f"{IDP_ID}~reader"]}
    source = (
        {"kind": "namespace", "name": "src", "impl": "dir", "properties": {"root": str(tmp_path / "catalog")}, "readers": readers}
        if kind == "namespace"
        else {"kind": "rask", "name": "src", "uri": catalog.url, "identity": "service", "readers": readers}
    )
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    key = tmp_path / "media-url-key"
    key.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    settings = _settings(
        dex,
        catalog,
        secret,
        WORKBENCH_SOURCES=[source],
        WORKBENCH_FGA_ENABLED=True,
        WORKBENCH_FGA_API_URL=openfga_store.url,
        WORKBENCH_FGA_STORE=openfga_store.name,
        WORKBENCH_FGA_MODEL_ID=openfga_store.model_id,
        VIEWER_MEDIA_URL_KEY_FILE=str(key),
        WORKBENCH_LAKEHOUSE_AUDIENCE_SCOPE=LAKEHOUSE_AUDIENCE_SCOPE,
    )
    openfga_store.write((user_object(f"{IDP_ID}~reader"), "reader", "source:src"))

    def bearer(sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {dex.mint(sub, audience=[WORKBENCH_AUDIENCE, LAKEHOUSE_AUDIENCE, 'workbench-app'], azp='workbench-app')}"}

    item = {"role": "cell", "table": "src~wh$ns$chunks", "column": "payload", "row": 0, "version": int(table.version)}
    try:
        with _viewer(settings, monkeypatch) as client:
            listed = {sub: [s["name"] for s in client.get("/api/datasets", headers=bearer(sub)).json()["sources"]] for sub in ("reader", "stranger")}
            opened = {
                sub: [
                    client.get(path, headers=bearer(sub)).status_code for path in ("/api/datasets/src~wh%24ns", "/api/datasets/src~wh%24ns/tables/chunks/rows")
                ]
                for sub in ("reader", "stranger")
            }
            derived = {
                sub: [
                    client.get(f"/api/datasets/src~wh%24ns/tables/chunks/{read}", params=picks, headers=bearer(sub)).status_code
                    for read, picks in DERIVED_READS
                ]
                for sub in ("reader", "stranger")
            }
            signing = {sub: client.post("/api/signed-media", json={"items": [item]}, headers=bearer(sub)) for sub in ("reader", "stranger")}
            link = str(signing["reader"].json()["urls"][0]["url"])
            served = client.get(link).status_code
            openfga_store.delete((user_object(f"{IDP_ID}~reader"), "reader", "source:src"))
            served_after = client.get(link).status_code
    finally:
        catalog.close()
        dex.close()

    assert (listed, opened) == ({"reader": ["src"], "stranger": []}, {"reader": [200, 200], "stranger": [403, 403]})
    assert derived == {"reader": [200] * len(DERIVED_READS), "stranger": [403] * len(DERIVED_READS)}
    assert ({sub: answer.status_code for sub, answer in signing.items()}, served, served_after) == ({"reader": 200, "stranger": 403}, 200, 403)


#: Reads of the default dataset, each naming something else beside it that is no dataset id the door reads, and the
#: rows of a dataset whose items are its rows, by the default and by name.
DEFAULT_READS = [
    "/api/doc-chunks/a",
    "/api/doc-chunks/a?table=junk",
    "/api/doc-chunks/a?corpus=junk",
    "/api/columns?table=x",
    "/api/rows",
    "/api/rows?table=junk",
    "/api/rows?dataset=src~wh%24ns",
]
#: The default dataset's text: longer than a listing answers of one cell (`browse.LISTED_TEXT_CHARS`).
LONG_TEXT = "a ship sails " * 700


def test_the_default_dataset_is_read_only_by_its_sources_readers_whatever_else_the_request_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: OpenFgaStore
) -> None:
    """A request naming no dataset reads the deployment's default, and the source checked is the one each read resolves:
    a query parameter that names no dataset the door reads (an unknown ``table``, a ``corpus`` it takes no notice of)
    neither stands in for the default nor hides it, so the declared reader is answered and a signed-in stranger refused.
    The reader's rows are kept by their browser alone, a text cell cut and a cell of bytes that are no UTF-8 answered as
    null, each named on its row."""
    dex = Provider("dex")
    root = tmp_path / "catalog"
    rows = pa.table({"doc_id": ["a"], "text": [LONG_TEXT], "blob": pa.array([b"\xff\xfe\x00"], pa.binary())})
    declared = {**_DESCRIPTOR, "display": {"title": ["doc_id"], "body": "text", "metadata": [{"field": "blob", "label": "Blob"}]}}
    write_table(root, ["wh", "ns", "chunks"], rows, descriptor=declared)
    settings = ViewerSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "src", "impl": "dir", "properties": {"root": str(root)}, "readers": {"people": [f"{IDP_ID}~reader"]}}
            ],
            "MEDIA_DEFAULT_DATASET": "src~wh$ns",
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
    try:
        with _viewer(settings, monkeypatch) as client:
            answered = {
                sub: [client.get(path, headers={"Authorization": f"Bearer {dex.mint(sub)}"}).status_code for path in DEFAULT_READS]
                for sub in ("reader", "stranger")
            }
            listed = client.get("/api/rows", headers={"Authorization": f"Bearer {dex.mint('reader')}"})
    finally:
        dex.close()

    assert answered == {"reader": [200] * len(DEFAULT_READS), "stranger": [403] * len(DEFAULT_READS)}
    [row] = listed.json()["items"]
    assert (listed.headers["cache-control"], row["text"], row["blob"], row["_cut"], row["_binary"]) == (
        "private, no-cache",
        LONG_TEXT[:8192],
        None,
        ["text"],
        ["blob"],
    )
