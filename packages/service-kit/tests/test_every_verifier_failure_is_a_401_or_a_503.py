"""A token or proof the door cannot use is answered 401 or 503, and never 500.

The door (`authenticate`) takes a token addressed to the workbench under the DPoP scheme and a proof of the key it is
bound to (RFC 9449). Every proof that breaks a rule of § 4.3, and a token bound to another key, is a 401 carrying a
`WWW-Authenticate: DPoP` challenge; so is a token the verifier refuses.

`OIDCVerifier.verify` has exactly two refusal types, and the door is built around them: `UnauthenticatedError` when the PRESENTED TOKEN is at fault (401, audited `invalid_token`), and
`ProviderUnavailableError` when the IdP, or this deployment's view of it, is at fault (503, audited
`verifier_unavailable`). Any third type — the fleet's `ServiceUnavailableError` included — reaches their `except
Exception` branch and is recorded as `invalid_token` against the caller, so each 503 case below also pins that its
raise site uses the subclass the Lance-taxonomy doors catch.

Measured 2026-09-25 on pyjwt 2.13.0 / httpx 0.28.1 / pydantic 2.13, these paths reach the door as a third type
unless `oidc.py` classifies them:

* a discovery document that is not JSON (a proxy's sign-in page) — `json.JSONDecodeError` out of `response.json()`;
* a key set that is not JSON — `json.JSONDecodeError` out of `PyJWKClient.fetch_data`, which classifies only
  transport failures;
* a key-set connection that closes before the status line, or a body cut short of its `Content-Length` —
  `http.client.RemoteDisconnected` / `IncompleteRead`, because urllib wraps only the SEND in `URLError`;
* key material `cryptography` refuses — a bare `ValueError`, because `PyJWKSet` skips only keys failing with
  `PyJWTError`;
* a key set PyJWT cannot read — an entry that is not an object (`AttributeError`), a non-string `n` or `alg`
  (`TypeError`), a body nested past the JSON parser's depth (`RecursionError`);
* a token whose `alg` names another key family than the key its `kid` selects — `TypeError` out of PyJWT's
  `prepare_key` when `jwt.decode` is handed the raw key rather than the `PyJWK` that binds its algorithm. This is the
  one a CALLER chooses, so it must be a 401.

The key-set cases also cover the failures PyJWT does type (`PyJWKClientConnectionError`, `PyJWKClientError`,
`PyJWKSetError`): none of them reads the token either, so they are the provider's too, and a 401 for them would tell a
caller holding a good bearer that it is bad — the same misattribution
`test_an_unreachable_idp_is_not_the_callers_fault.py` refuses for the discovery document.

THE IdP IS A REAL HTTP SERVER ON LOOPBACK. Neither httpx nor urllib is patched, so both fetches run the libraries'
own parsing — which is where every one of these failures is raised. A stubbed response object would replace exactly
the code under test.
"""

import base64
import hashlib
import json
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any, cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from service_kit.exceptions import register_handlers
from service_kit.governed import dpop as dpop_mod
from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.governed.deps import make_auth_deps
from service_kit.governed.dpop import ALGORITHMS, DpopVerifier
from service_kit.governed.identity import PersonVerifier
from service_kit.governed.oidc import UNKNOWN_KID_REFRESH_INTERVAL_SECONDS, OIDCVerifier
from service_kit.governed.settings import ClientMarker, OidcProvider
from service_kit.governed.token_exchange import LakehouseCaller
from service_kit.ns_errors import install_problem_handlers
from tests.identity import BrowserKey, Provider


AUDIENCE = "workbench"
KID = "signing-1"
DOORS = ["/gated"]
URL = "http://testserver/gated"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/jwks"

SLOW_ANSWER_SECONDS = 0.3

#: What a reverse proxy in front of an IdP answers when its session is gone: a 200 whose body is a page.
SIGN_IN_PAGE = b"<!doctype html><title>Sign in</title>"


class _AuthSettings(BaseModel):
    oidc_enabled: bool = True
    dpop_required: bool = True
    fga_enabled: bool = False
    public_url: str | None = None
    public_mount: str = ""
    reads_lakehouse_as_person: bool = False


def _auth_settings() -> _AuthSettings:
    return _AuthSettings()


# Module scope: `make_auth_deps` must be bound where FastAPI can resolve the route annotations
# (`test_auth_deps_resolve.py` records why a local binding silently degrades to a query parameter).
_deps = make_auth_deps(Annotated[_AuthSettings, Depends(_auth_settings)])
_Subject = Annotated[str, Depends(_deps.current_subject)]
_AsPerson = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]

#: The browser's key every token below is bound to, unless a case binds one to another.
BROWSER = BrowserKey()


class _Fault(StrEnum):
    """An IdP that goes away mid-answer — a pod restarting while keys rotate."""

    CLOSED_BEFORE_ANSWERING = "closed-before-answering"
    BODY_CUT_SHORT = "body-cut-short"
    #: A pod killed mid-response: the reader gets a bare `ConnectionResetError`, an `OSError` that is not also an
    #: `http.client.HTTPException` — the one case only `_key_set`'s `OSError` member catches.
    RESET_MID_BODY = "reset-mid-body"


class _IdP(BaseModel):
    """What the loopback IdP serves, by path; a case overwrites one document, or faults one path, to break it."""

    issuer: str
    documents: dict[str, bytes]
    faults: dict[str, _Fault] = {}
    #: Paths answered after `SLOW_ANSWER_SECONDS`: long enough that every request in a concurrent burst reaches the
    #: verifier while the first one's fetch is still in flight.
    slow: set[str] = set()
    #: Requests served, by path.
    fetches: dict[str, int] = {}


def _b64url_uint(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _discovery(issuer: str, *, jwks_path: str = JWKS_PATH, algorithms: tuple[str, ...] = ("RS256", "ES256")) -> bytes:
    """Both key families advertised by default, as Keycloak does and as an IdP advertising none implies.

    Both matter: `verify` intersects the advertised algorithms with its allowlist, so an IdP advertising only RS256
    would refuse an ES256 header before any key is prepared, and the family-mismatch case could not arise.
    """
    return json.dumps({"issuer": issuer, "jwks_uri": f"{issuer}{jwks_path}", "id_token_signing_alg_values_supported": list(algorithms)}).encode()


def _key_set(key: rsa.RSAPrivateKey, kid: str) -> bytes:
    public = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64url_uint(public.n), "e": _b64url_uint(public.e)}
    return json.dumps({"keys": [jwk]}).encode()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def stranger_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(rsa_key: rsa.RSAPrivateKey) -> Iterator[_IdP]:
    #: Connections whose close must be a reset rather than an orderly FIN.
    resets: set[socket.socket] = set()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.fetches[self.path] = served.fetches.get(self.path, 0) + 1
            fault = served.faults.get(self.path)
            body = served.documents.get(self.path)
            if self.path in served.slow:
                time.sleep(SLOW_ANSWER_SECONDS)
            if fault is _Fault.CLOSED_BEFORE_ANSWERING:
                self.close_connection = True
                return
            if fault in (_Fault.BODY_CUT_SHORT, _Fault.RESET_MID_BODY) and body is not None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.close_connection = True
                if fault is _Fault.RESET_MID_BODY:
                    resets.add(self.connection)
                return
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own parameter name
            return

    class _Server(ThreadingHTTPServer):
        def shutdown_request(self, request: socket.socket | tuple[bytes, socket.socket]) -> None:
            if not isinstance(request, socket.socket) or request not in resets:
                super().shutdown_request(request)
                return
            resets.discard(request)
            # A zero linger makes close() send RST. The base sends SHUT_WR first, whose FIN would reach the reader
            # ahead of the reset and read as a short body.
            request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_request(request)

    server = _Server(("127.0.0.1", 0), _Handler)
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    served = _IdP(issuer=issuer, documents={DISCOVERY_PATH: _discovery(issuer), JWKS_PATH: _key_set(rsa_key, KID)})
    # `shutdown()` waits out one poll of `serve_forever`; at the 0.5 s default that is most of this file's runtime.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()


def _token(
    idp: _IdP,
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    algorithm: str,
    *,
    kid: str | None = KID,
    jkt: str | None = BROWSER.jkt,
    extra: dict[str, object] | None = None,
) -> str:
    now = int(time.time())
    claims = {"iss": idp.issuer, "sub": "gina", "aud": AUDIENCE, "iat": now, "exp": now + 300} | ({"cnf": {"jkt": jkt}} if jkt else {}) | (extra or {})
    return jwt.encode(claims, key, algorithm=algorithm, headers={} if kid is None else {"kid": kid})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(idp: _IdP, *, clock: Callable[[], float] = time.monotonic, further: OidcProvider | None = None, **naming: Any) -> TestClient:
    """A governed app wired as every governed service wires one: BOTH handler installers, one verifier, its issuer's
    people named by `naming`'s rule (`OidcProvider`'s fields), the idp id `corp` by default, and a `further` issuer."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    providers = [OidcProvider(issuer=idp.issuer, idp_id="corp", **naming), *([further] if further else [])]
    app.state.oidc = PersonVerifier(
        OIDCVerifier([provider.issuer for provider in providers], AUDIENCE, cache_ttl=300, allow_insecure=True, clock=clock), providers
    )
    app.state.dpop = DpopVerifier()

    @app.get("/gated")
    def _gated(subject: _Subject) -> dict[str, str]:
        return {"subject": subject}

    @app.get("/as-person")
    def _as_person(caller: _AsPerson) -> dict[str, str]:
        return {"subject": caller.subject}

    return TestClient(app, raise_server_exceptions=False)


def _call(idp: _IdP, door: str, token: str, *, client: TestClient | None = None) -> tuple[int, str]:
    proof = BROWSER.proof("GET", f"http://testserver{door}", token)
    response = (client or _client(idp)).get(door, headers={"Authorization": f"DPoP {token}", "DPoP": proof})
    return response.status_code, response.text


@pytest.fixture
def audit_trail(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _audited_reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "audit.reason", None) for record in caplog.records if record.name == AUDIT_LOGGER]


# ── the controls: the fix must not blur the line between the two refusals ─────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_token_the_key_set_signed_is_accepted(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 200, body
    assert json.loads(body) == {"subject": "corp~gina"}


#: How a token names its person, as the lakehouse names them: (the issuer's naming rule, the token's own claims, the
#: principal), or `None` for a token that names no person. The issuer's `sub` is the case above, and a mapped Dex
#: connector the annotator's sign-in at a Dex-shaped provider
#: (`services/annotator/tests/test_a_person_signs_in_at_either_shape_of_provider.py`).
NAMING = {
    "a-named-subject-claim": ({"subject_claims": ("oid", "sub")}, {"oid": "o-1"}, "corp~o-1"),
    "a-dex-connector-unmapped": ({"connectors": {"local": "dex"}}, {"federated_claims": {"connector_id": "ldap", "user_id": "u-1"}}, None),
    "a-dex-token-without-federated-claims": ({"connectors": {"local": "dex"}}, {}, None),
    "a-dex-client-for-itself": ({}, {"sub": "Cgl3b3JrYmVuY2g", "azp": "workbench"}, None),
    "an-rfc-9068-client": ({}, {"sub": "workbench", "client_id": "workbench"}, None),
    "a-marked-client": ({"client_marker": ClientMarker(claim="client_id")}, {"client_id": "workbench"}, None),
    "a-sub-that-is-its-own-audience": ({}, {"sub": AUDIENCE}, None),
    "a-sub-that-is-its-authorized-party": ({}, {"sub": "workbench-app", "azp": "workbench-app"}, None),
}


@pytest.mark.parametrize(("naming", "claims", "principal"), NAMING.values(), ids=NAMING.keys())
def test_a_token_names_its_person_as_the_lakehouse_does_and_a_clients_names_none(
    naming: dict[str, Any], claims: dict[str, object], principal: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey
) -> None:
    status, body = _call(idp, "/gated", _token(idp, rsa_key, "RS256", extra=claims), client=_client(idp, **naming))

    assert (status, json.loads(body).get("subject")) == ((200, principal) if principal else (401, None)), body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_burst_of_novel_kids_costs_one_key_set_fetch(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """A `kid` is the caller's to write, unverified. Each novel one earns a refetch only while no other began within the
    interval, so a flood of them, anonymous and concurrent, costs the IdP one fetch rather than one per request, and the
    requests behind it are refused at once rather than each holding a worker for the fetch budget ([[LH-431]]). The IdP
    answers slowly so that the whole burst meets the verifier while the first refetch is in flight: a pace claimed only
    once its fetch returns lets every request in the burst through."""
    client = _client(idp)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.slow.add(JWKS_PATH)
    tokens = [_token(idp, rsa_key, "RS256", kid=f"forged-{n}") for n in range(8)]

    with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
        statuses = list(pool.map(lambda token: _call(idp, door, token, client=client)[0], tokens))

    assert statuses == [401] * len(tokens)
    assert idp.fetches[JWKS_PATH] == 2


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_rotated_in_during_a_flood_is_found_once_the_interval_passes(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """A key the provider rotates in after the cached set is found by a refetch, which a forged kid met first may delay by
    at most one interval and never lock out: refusing a rotated token from the cache would lock every caller out for
    the cache lifetime after each rotation. Three fetches in all: the first request's, the forged kid's, and the
    rotated token's once the interval has passed."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    assert _call(idp, door, _token(idp, rsa_key, "RS256", kid="forged"), client=client)[0] == 401
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2")
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS

    status, body = _call(idp, door, _token(idp, stranger_key, "RS256", kid="signing-2"), client=client)

    assert status == 200, body
    assert idp.fetches[JWKS_PATH] == 3


# ── the caller's fault ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_token_signed_by_another_key_is_the_callers(door: str, idp: _IdP, stranger_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    status, body = _call(idp, door, _token(idp, stranger_key, "RS256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


@pytest.mark.parametrize("kid", ["retired-key", None], ids=["unknown-kid", "no-kid"])
@pytest.mark.parametrize("door", ["/gated"])
def test_a_kid_the_key_set_does_not_hold_is_the_callers(
    door: str, kid: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    """The key set is fine; the token names a key it does not hold (even after the one refetch a rotation earns), or
    names none at all."""
    status, body = _call(idp, door, _token(idp, rsa_key, "RS256", kid=kid))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_token_whose_alg_names_another_key_family_is_the_callers(door: str, idp: _IdP, audit_trail: pytest.LogCaptureFixture) -> None:
    """ES256 in the header, the RSA key's `kid` beside it. Both values are the caller's to write, and RFC 8725 §3.1
    requires each key to be used with exactly one algorithm — so this is a refused token, not a fault."""
    status, body = _call(idp, door, _token(idp, ec.generate_private_key(ec.SECP256R1()), "ES256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


# ── the IdP's fault, which is ours ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_discovery_url_that_answers_404_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    del idp.documents[DISCOVERY_PATH]

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "could not be reached" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_discovery_document_naming_another_issuer_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The document disagrees with this deployment's configuration; the token is never read."""
    idp.documents[DISCOVERY_PATH] = _discovery("https://another-issuer.example.test")

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "different issuer" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_provider_advertising_no_algorithm_we_accept_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The provider's advertised list against this deployment's allowlist; the token's own `alg` plays no part."""
    idp.documents[DISCOVERY_PATH] = _discovery(idp.issuer, algorithms=("PS256",))

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "signing algorithm" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_discovery_document_that_is_not_json_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[DISCOVERY_PATH] = SIGN_IN_PAGE

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_url_that_answers_404_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The discovery document is sound and names a key-set URL nothing serves."""
    idp.documents[DISCOVERY_PATH] = _discovery(idp.issuer, jwks_path="/nothing-here")

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "could not be reached" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("fault", list(_Fault))
@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_the_idp_drops_mid_answer_is_ours(
    door: str, fault: _Fault, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    """Named unreachable, not unusable: the set never arrived, so nothing is known about its keys."""
    idp.faults[JWKS_PATH] = fault

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "could not be reached" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


#: A zero modulus: every field of the right type, and a key `cryptography` will not build.
_ZERO_MODULUS_JWK = {"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256", "n": "AA", "e": "AQAB"}

#: Key-set bodies the provider could serve, none of which the token has any part in.
UNUSABLE_KEY_SETS = {
    "not-json": SIGN_IN_PAGE,
    "no-keys": json.dumps({"keys": []}).encode(),
    "entry-not-an-object": json.dumps({"keys": [1]}).encode(),
    "n-not-a-string": json.dumps({"keys": [{**_ZERO_MODULUS_JWK, "n": 5}]}).encode(),
    # `RecursionError`, a family none of the others raise.
    "nested-past-the-parsers-depth": b"[" * 100_000 + b"]" * 100_000,
}


@pytest.mark.parametrize("key_set", UNUSABLE_KEY_SETS.values(), ids=UNUSABLE_KEY_SETS.keys())
@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_with_no_usable_key_is_ours(door: str, key_set: bytes, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[JWKS_PATH] = key_set

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert "holds no usable signing key" in json.loads(body)["detail"]
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("key_set", [UNUSABLE_KEY_SETS["not-json"], UNUSABLE_KEY_SETS["no-keys"]], ids=["not-json", "no-keys"])
@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_the_idp_has_since_fixed_is_believed_at_once(door: str, key_set: bytes, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """`PyJWKClient` caches a body before parsing it, so an unusable set it cached would otherwise be served for the
    cache's whole lifespan (300 s) after the provider replaced it — every caller refused for an outage that is over."""
    client = _client(idp)
    token = _token(idp, rsa_key, "RS256")
    usable = idp.documents[JWKS_PATH]
    idp.documents[JWKS_PATH] = key_set
    assert _call(idp, door, token, client=client)[0] == 503

    idp.documents[JWKS_PATH] = usable
    status, body = _call(idp, door, token, client=client)

    assert status == 200, body
    # Two fetches in all: the first request's, and the second request's, which finds the fixed set. That second is a
    # refetch for a body the cache kept, and a first read for one never cached (not JSON, or nested past the parser's depth).
    assert idp.fetches[JWKS_PATH] == 2


@pytest.mark.parametrize("key_set", [UNUSABLE_KEY_SETS["not-json"], UNUSABLE_KEY_SETS["no-keys"]], ids=["not-json", "no-keys"])
@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_that_arrived_unusable_is_fetched_once_per_request(door: str, key_set: bytes, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The refetch is for a set the CACHE served. One fetched in this request is already fresh, and asking again only
    doubles the load on an IdP serving garbage and the time each request is held."""
    idp.documents[JWKS_PATH] = key_set
    client = _client(idp)
    token = _token(idp, rsa_key, "RS256")

    fetched = []
    for _ in range(2):
        assert _call(idp, door, token, client=client)[0] == 503
        fetched.append(idp.fetches[JWKS_PATH])

    assert fetched == [1, 2]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_that_cannot_be_reached_is_asked_for_once(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The refetch above is for a set that ARRIVED unusable. One that never arrived is not asked for twice: against a
    hung IdP each attempt waits out the fetch timeout, so a second would double the time a request is held."""
    idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING

    assert _call(idp, door, _token(idp, rsa_key, "RS256"))[0] == 503
    assert idp.fetches[JWKS_PATH] == 1


# ── the proof of possession (RFC 9449 § 4.3, § 7) ─────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _forged(header: dict[str, object], claims: dict[str, object]) -> str:
    """A compact JWS whose header and claims are the caller's to write, signed by a key the header does not name."""
    stranger = BrowserKey()
    signed = stranger.proof("GET", URL, None).split(".")[2]
    return ".".join([_b64(json.dumps(header).encode()), _b64(json.dumps(claims).encode()), signed])


def _claims(token: str, **over: object) -> dict[str, object]:
    ath = _b64(hashlib.sha256(token.encode()).digest())
    return {"jti": f"jti-{time.monotonic_ns()}", "htm": "GET", "htu": URL, "iat": int(time.time()), "ath": ath} | over


PUBLIC_JWK = BROWSER._key.as_dict(private=False)

#: Each case breaks one rule; the proof is otherwise the browser's own, for a token bound to the browser's key.
BROKEN_PROOFS: dict[str, Callable[[str], str]] = {
    "typ-is-not-dpop-jwt": lambda token: BROWSER.proof("GET", URL, token, header={"typ": "JWT"}),
    "alg-is-symmetric": lambda token: _forged({"typ": "dpop+jwt", "alg": "HS256", "jwk": PUBLIC_JWK}, _claims(token)),
    "no-jwk": lambda token: _forged({"typ": "dpop+jwt", "alg": "ES256"}, _claims(token)),
    "jwk-carries-the-private-key": lambda token: BROWSER.proof("GET", URL, token, header={"jwk": BROWSER._key.as_dict(private=True)}),
    "signed-by-another-key": lambda token: _forged({"typ": "dpop+jwt", "alg": "ES256", "jwk": PUBLIC_JWK}, _claims(token)),
    "htm-is-another-method": lambda token: BROWSER.proof("POST", URL, token),
    "htu-is-another-url": lambda token: BROWSER.proof("GET", "http://testserver/elsewhere", token),
    "iat-outside-the-window": lambda token: BROWSER.proof("GET", URL, token, iat=int(time.time()) - 3600),
    "ath-of-another-token": lambda token: BROWSER.proof("GET", URL, token + "x"),
    "no-ath": lambda token: BROWSER.proof("GET", URL, None),
    "no-jti": lambda token: BROWSER.proof("GET", URL, token, jti=None),
    "not-a-jws": lambda token: "not.a.proof",
}


@pytest.mark.parametrize("make_proof", BROKEN_PROOFS.values(), ids=BROKEN_PROOFS.keys())
def test_a_proof_that_breaks_a_rule_is_refused_with_a_dpop_challenge(
    make_proof: Callable[[str], str], idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    token = _token(idp, rsa_key, "RS256")

    response = _client(idp).get("/gated", headers={"Authorization": f"DPoP {token}", "DPoP": make_proof(token)})

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"].startswith('DPoP error="invalid_dpop_proof", algs="')
    assert _audited_reasons(audit_trail) == ["invalid_dpop_proof"]


@pytest.mark.parametrize("jkt", [BrowserKey().jkt, None], ids=["bound-to-another-key", "bound-to-no-key"])
def test_a_token_not_bound_to_the_proofs_key_is_refused(jkt: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _token(idp, rsa_key, "RS256", jkt=jkt)

    status, body = _call(idp, "/gated", token)

    assert status == 401, body
    assert "not bound to the DPoP proof's key" in json.loads(body)["detail"]


def test_a_proof_sent_twice_is_refused_the_second_time(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The replay store is this process's: the stated limit is a replay to another replica, which it cannot see."""
    client = _client(idp)
    token = _token(idp, rsa_key, "RS256")
    headers = {"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", URL, token)}

    first, second = client.get("/gated", headers=headers), client.get("/gated", headers=headers)

    assert (first.status_code, second.status_code) == (200, 401)
    assert "already used" in second.json()["detail"]


DOOR_REFUSALS = {
    "no-token": ({}, None),
    "bearer-scheme": ({"Authorization": "Bearer {token}", "DPoP": "{proof}"}, "invalid_token"),
    "no-proof": ({"Authorization": "DPoP {token}"}, "invalid_dpop_proof"),
}


@pytest.mark.parametrize(("headers", "error"), DOOR_REFUSALS.values(), ids=DOOR_REFUSALS.keys())
def test_a_request_without_a_dpop_bound_token_and_its_proof_is_challenged(
    headers: dict[str, str], error: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey
) -> None:
    # Bound to no key: a deployment that requires a proof refuses a bearer whether or not the token names a key.
    token = _token(idp, rsa_key, "RS256", jkt=None)
    proof = BROWSER.proof("GET", URL, token)

    response = _client(idp).get("/gated", headers={k: v.format(token=token, proof=proof) for k, v in headers.items()})

    assert response.status_code == 401
    algs = f'algs="{" ".join(sorted(ALGORITHMS))}"'
    assert response.headers["www-authenticate"] == (f'DPoP error="{error}", {algs}' if error else f"DPoP {algs}")


_DPOP_ALGS = f'algs="{" ".join(sorted(ALGORITHMS))}"'
_BEARER_REFUSED = f'Bearer error="invalid_token", DPoP {_DPOP_ALGS}'
_PROOF_REFUSED = f'Bearer, DPoP error="invalid_dpop_proof", {_DPOP_ALGS}'

#: Where the provider binds no token: how a token is presented, its confirmation claim, whether a proof goes with it,
#: and the status and challenge it is answered. Any `cnf` at all needs its proof. A bearer bound to no key is accepted
#: there: the sign-in at a Dex-shaped provider in the annotator's and the viewer's suites.
UNBOUND_PROVIDER = {
    "a-bearer-bound-to-a-key": ("Bearer", {"jkt": BROWSER.jkt}, False, 401, _BEARER_REFUSED),
    "a-bearer-confirmed-by-a-certificate": ("Bearer", {"x5t#S256": "certificate-thumbprint"}, False, 401, _BEARER_REFUSED),
    "a-bearer-with-a-null-confirmation": ("Bearer", None, False, 401, _BEARER_REFUSED),
    "a-bound-token-with-its-proof": ("DPoP", {"jkt": BROWSER.jkt}, True, 200, None),
    "a-bound-token-without-its-proof": ("DPoP", {"jkt": BROWSER.jkt}, False, 401, _PROOF_REFUSED),
    "a-dpop-token-bound-to-no-key": ("DPoP", "absent", True, 401, _PROOF_REFUSED),
}


@pytest.mark.parametrize(("scheme", "cnf", "with_proof", "status", "challenge"), UNBOUND_PROVIDER.values(), ids=UNBOUND_PROVIDER.keys())
def test_where_the_provider_binds_no_token_a_bearer_is_accepted_only_when_it_names_no_key(
    scheme: str, cnf: object, with_proof: bool, status: int, challenge: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey
) -> None:
    client = _client(idp)
    cast(FastAPI, client.app).dependency_overrides[_auth_settings] = lambda: _AuthSettings(dpop_required=False)
    token = _token(idp, rsa_key, "RS256", jkt=None, extra=None if cnf == "absent" else {"cnf": cnf})
    proof = {"DPoP": BROWSER.proof("GET", URL, token)} if with_proof else {}

    response = client.get("/gated", headers={"Authorization": f"{scheme} {token}"} | proof)

    assert response.status_code == status, response.text
    assert response.headers.get("www-authenticate") == challenge


def test_a_principal_that_reads_as_a_machine_is_never_a_person_whatever_the_issuers_rule(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """Settings refuse a machine's idp id for people; the door holds the line too, for a rule built around them."""
    app_client = _client(idp)
    unchecked = OidcProvider.model_construct(issuer=idp.issuer, idp_id="corp-client", connectors={}, subject_claims=None, client_marker=None)
    cast(FastAPI, app_client.app).state.oidc = PersonVerifier(OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True), [unchecked])

    status, _body = _call(idp, "/gated", _token(idp, rsa_key, "RS256"), client=app_client)

    assert status == 401


def test_a_token_from_a_further_issuer_is_named_by_that_issuers_own_rule(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    further = Provider()
    client = _client(idp, further=OidcProvider(issuer=further.issuer, idp_id="further", allow_insecure=True))
    token = further.mint("ada", audience=AUDIENCE, jkt=BROWSER.jkt)
    try:
        status, body = _call(idp, "/gated", token, client=client)
    finally:
        further.close()

    assert (status, json.loads(body)) == (200, {"subject": "further~ada"})


def test_a_token_naming_an_issuer_the_deployment_does_not_is_refused_and_its_issuer_never_asked(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    stranger = Provider()
    token = stranger.mint("ada", audience=AUDIENCE, jkt=BROWSER.jkt)
    try:
        status, _body = _call(idp, "/gated", token)
    finally:
        stranger.close()

    assert (status, stranger.fetches) == (401, [])


def test_behind_a_gateway_that_rewrites_the_prefix_the_proof_names_the_public_url(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The browser signs the URL it addresses; the service sees the path the gateway rewrote it to."""
    public = "https://workbench.example/api/explorer/annotations"
    client = _client(idp)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[_auth_settings] = lambda: _AuthSettings(public_url=public, public_mount="/gated")
    token = _token(idp, rsa_key, "RS256")

    as_signed = client.get("/gated", headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", public, token)})
    # RFC 3986 § 6.2.3: a URL naming its scheme's default port is the same URL.
    with_port = client.get(
        "/gated", headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", public.replace(".example/", ".example:443/"), token)}
    )
    as_seen = client.get("/gated", headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", URL, token)})

    assert (as_signed.status_code, with_port.status_code, as_seen.status_code) == (200, 200, 401)


def test_one_persons_flood_of_proofs_does_not_make_the_store_forget_anothers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each person fills a bucket of their own, so a stolen proof stays refused however many proofs someone else sends."""
    monkeypatch.setattr(dpop_mod, "REPLAY_PER_PERSON", 2)
    store = dpop_mod.ReplayStore(window_seconds=60)
    assert store.first_use("corp~gina", "stolen")

    flooded = [store.first_use("corp~mallory", f"flood-{n}") for n in range(10)]

    assert all(flooded) and not store.first_use("corp~gina", "stolen")


def test_a_lakehouse_read_as_a_person_with_no_client_to_exchange_their_token_is_refused_not_anonymous(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    client = _client(idp)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[_auth_settings] = lambda: _AuthSettings(reads_lakehouse_as_person=True)
    token = _token(idp, rsa_key, "RS256")
    url = "http://testserver/as-person"

    response = client.get("/as-person", headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", url, token)})

    assert response.status_code == 503
    assert "WORKBENCH_OIDC_CLIENT_ID" in response.json()["detail"]
