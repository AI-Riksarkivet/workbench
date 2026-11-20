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
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any, cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from service_kit.exceptions import register_handlers
from service_kit.governed import dpop as dpop_mod
from service_kit.governed import oidc as oidc_mod
from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.governed.deps import make_auth_deps
from service_kit.governed.dpop import ALGORITHMS, DpopVerifier
from service_kit.governed.identity import PersonVerifier
from service_kit.governed.oidc import (
    ABANDONED_READS,
    FETCH_RETRY_SECONDS,
    FORCED_WAITERS,
    KEY_SET_MAX_AGE_SECONDS,
    KEY_SET_STALE_LIMIT_SECONDS,
    MAX_DOCUMENT_BYTES,
    RETIRE_AFTER_READS,
    RETIRED_KID_SECONDS,
    RETIRED_KIDS_PER_ISSUER,
    UNKNOWN_KID_REFRESH_INTERVAL_SECONDS,
    OIDCVerifier,
)
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
DRIP_SECONDS = 0.02
#: How long a held answer, or a wait for the refresher, may last before the test is failed as hung: a safety bound on a
#: starved host, never what a test measures.
HELD_SECONDS = 30.0
#: The attempt deadline of a test whose issuer stalls one read: wide enough that the loopback reads the test needs to
#: succeed do so on a starved host, and still far below `HELD_SECONDS`, so the stalled read is ended by its deadline.
CUT_DEADLINE_SECONDS = 2.0

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

    model_config = ConfigDict(arbitrary_types_allowed=True)

    issuer: str
    documents: dict[str, bytes]
    faults: dict[str, _Fault] = {}
    #: Paths answered after `SLOW_ANSWER_SECONDS`: long enough that every request in a concurrent burst reaches the
    #: verifier while the first one's fetch is still in flight.
    slow: set[str] = set()
    #: Paths whose NEXT request is answered a byte every `DRIP_SECONDS`, without end, while every later one is answered
    #: as usual: an issuer that holds one connection open and serves new ones.
    drip: set[str] = set()
    #: Paths whose next n requests are answered a status line and then one header line every `DRIP_SECONDS`, without
    #: end: no per-operation timeout ever trips, and the response never begins.
    drip_headers: dict[str, int] = {}
    #: Paths answered 302 to another path.
    redirects: dict[str, str] = {}
    #: Paths answered from these bodies in turn, one request each, in place of `documents`: replicas behind one address
    #: that serve different documents.
    turns: dict[str, list[bytes]] = {}
    #: Paths answered only once the test sets `release` (at the latest `HELD_SECONDS`, which bounds a hung test alone).
    held: set[str] = set()
    release: threading.Event = Field(default_factory=threading.Event)
    #: Connections a dripping path was writing to when its reader shut them down, by path.
    cuts: dict[str, int] = {}
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
    stopped = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.fetches[self.path] = served.fetches.get(self.path, 0) + 1
            fault = served.faults.get(self.path)
            body = served.documents.get(self.path)
            if turn := served.turns.get(self.path):
                body = turn[0]
                turn.append(turn.pop(0))
            if served.drip_headers.get(self.path, 0) > 0:
                served.drip_headers[self.path] -= 1
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    while not stopped.is_set():
                        self.wfile.write(b"X-Drip: 1\r\n")
                        self.wfile.flush()
                        time.sleep(DRIP_SECONDS)
                except OSError:
                    served.cuts[self.path] = served.cuts.get(self.path, 0) + 1
                self.close_connection = True
                return
            if self.path in served.redirects:
                self.send_response(302)
                self.send_header("Location", f"{served.issuer}{served.redirects[self.path]}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path in served.drip:
                served.drip.discard(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(1 << 30))
                self.end_headers()
                try:
                    while not stopped.is_set():
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(DRIP_SECONDS)
                except OSError:
                    served.cuts[self.path] = served.cuts.get(self.path, 0) + 1
                self.close_connection = True
                return
            if self.path in served.held:
                served.release.wait(HELD_SECONDS)
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
        stopped.set()
        served.release.set()
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


#: The verifiers this module's apps hold, closed when each test ends, as an app's shutdown closes its own.
_verifiers: list[OIDCVerifier] = []


@pytest.fixture(autouse=True)
def _refresher_at_test_pace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The refresher looks at whether an issuer is due every 20 ms rather than every half second, so a test that moves
    its clock past an age sees the refresh at once; and every verifier a test built is closed when it ends."""
    monkeypatch.setattr(oidc_mod, "REFRESHER_TICK_SECONDS", 0.02)
    yield
    while _verifiers:
        _verifiers.pop().close()


def _until(done: Callable[[], bool], timeout: float = HELD_SECONDS) -> None:
    """Wait for the refresher's work, which runs on a thread of its own, until ``done``: polled, with ``timeout`` only
    bounding a test that hangs (it is no measure of how soon the work is done, which a starved host stretches)."""
    deadline = time.monotonic() + timeout
    while not done():
        assert time.monotonic() < deadline, "the refresher did not get there in time"
        time.sleep(0.01)


def _until_held_at(idp: _IdP, at: float) -> None:
    """Wait until the newest verifier holds a key set of ``idp`` its refresher read at clock time ``at``: the read
    served AND recorded, so a request after it is judged on that set."""
    state = _verifiers[-1]._state[idp.issuer]
    _until(lambda: state.keys is not None and state.keys.fetched_at == at)


def _client(
    idp: _IdP, *, clock: Callable[[], float] = time.monotonic, further: OidcProvider | None = None, discovery_ttl: int = 300, **naming: Any
) -> TestClient:
    """A governed app wired as every governed service wires one: BOTH handler installers, one verifier, its issuer's
    people named by `naming`'s rule (`OidcProvider`'s fields), the idp id `corp` by default, and a `further` issuer."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    providers = [OidcProvider(issuer=idp.issuer, idp_id="corp", **naming), *([further] if further else [])]
    verifier = OIDCVerifier([provider.issuer for provider in providers], AUDIENCE, cache_ttl=discovery_ttl, allow_insecure=True, clock=clock)
    _verifiers.append(verifier)
    app.state.oidc = PersonVerifier(verifier, providers)
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
    """A `kid` is the caller's to write, unverified. A flood of novel ones, anonymous and concurrent, costs the IdP one
    forced refresh rather than one per request ([[LH-431]]); at most `FORCED_WAITERS` of them wait for it and are
    refused once the set it read is found without their `kid`, and the rest are told at once to come back (503) rather
    than each holding a worker for the fetch budget. The IdP answers slowly so that the whole burst meets the verifier
    while the refresh is in flight."""
    client = _client(idp)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.slow.add(JWKS_PATH)
    tokens = [_token(idp, rsa_key, "RS256", kid=f"forged-{n}") for n in range(FORCED_WAITERS + 4)]

    answers = _timed_burst(idp, door, tokens, client)

    refused = [took for status, took in answers if status == 401]
    others = {status for status, _ in answers if status != 401}
    assert (len(refused) <= FORCED_WAITERS, others, idp.fetches[JWKS_PATH]) == (True, {503}, 2)


def _timed_burst(idp: _IdP, door: str, tokens: list[str], client: TestClient) -> list[tuple[int, float]]:
    """Each of ``tokens`` presented at once: each one's status and how long it took."""

    def timed(token: str) -> tuple[int, float]:
        began = time.monotonic()
        return _call(idp, door, token, client=client)[0], time.monotonic() - began

    with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
        return list(pool.map(timed, tokens))


@pytest.mark.parametrize("door", ["/gated"])
def test_a_page_of_requests_signed_by_a_key_rotated_in_all_wait_for_one_refresh_and_are_accepted(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """The issuer restarts with a new key and the person signs in again: their page's reads arrive together, each naming
    a key the set held does not. One forced refresh is read, every one of them waits for it, and every one is let in,
    rather than all but the first being refused and the person asked to sign in once more."""
    client = _client(idp)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "after-restart")
    idp.slow.add(JWKS_PATH)
    tokens = [_token(idp, stranger_key, "RS256", kid="after-restart") for _ in range(6)]

    answers = _timed_burst(idp, door, tokens, client)

    assert ([status for status, _ in answers], idp.fetches[JWKS_PATH]) == ([200] * 6, 2)


@pytest.mark.parametrize("door", ["/gated"])
def test_the_requests_waiting_for_a_forced_refresh_that_hangs_are_answered_within_its_deadline(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced refresh the issuer does not answer is abandoned at its deadline, and every request waiting for it is
    answered then, not held for the issuer's own time: told to come back (503), since no key set was read that could
    say the token is bad. The issuer holds its answer until the test has every request's: the requests were answered,
    and the attempt ended, while the issuer had still not answered."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    monkeypatch.setattr(oidc_mod, "HTTP_FETCH_TIMEOUT_SECONDS", 60.0)
    client = _client(idp)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    verifier = _verifiers[-1]
    idp.held.add(JWKS_PATH)
    tokens = [_token(idp, rsa_key, "RS256", kid="not-yet-read") for _ in range(FORCED_WAITERS)]

    answers = _timed_burst(idp, door, tokens, client)
    ended_unanswered = (verifier._state[idp.issuer].attempts, idp.release.is_set())
    idp.release.set()

    assert ([status for status, _ in answers], ended_unanswered) == ([503] * FORCED_WAITERS, (2, False))


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_rotated_in_during_a_flood_is_found_once_the_interval_passes(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """A key the provider rotates in after the cached set is found by a refetch, which a forged kid met first may delay by
    at most one interval and never lock out: within it the rotated token is told when to come back (503 with the time
    left of the interval), never refused, so the person's session is not ended by someone else's forgery. Three fetches
    in all: the first request's, the forged kid's, and the rotated token's once the interval has passed."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    assert _call(idp, door, _token(idp, rsa_key, "RS256", kid="forged"), client=client)[0] == 401
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2")
    rotated = _token(idp, stranger_key, "RS256", kid="signing-2")
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS / 3
    within = client.get(door, headers={"Authorization": f"DPoP {rotated}", "DPoP": BROWSER.proof("GET", URL, rotated)})
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS * 2 / 3

    status, body = _call(idp, door, rotated, client=client)

    assert (within.status_code, within.headers.get("retry-after"), status, idp.fetches[JWKS_PATH]) == (
        503,
        str(int(UNKNOWN_KID_REFRESH_INTERVAL_SECONDS * 2 / 3)),
        200,
        3,
    ), body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_rotated_in_whose_forced_read_fails_is_told_to_come_back_and_is_let_in_once_the_issuer_answers(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """The person signs in again at an issuer that restarted, and the forced read their new key asks for fails: no set
    was read that could say the token is bad, so it is a 503 saying when to come back, not a 401 that would end their
    session; the refresher asks again at the retry pace, and the token is let in once the issuer answers."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "after-restart")
    idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    token = _token(idp, stranger_key, "RS256", kid="after-restart")

    first, then = (client.get(door, headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", URL, token)}) for _ in range(2))
    del idp.faults[JWKS_PATH]
    clock.now += FETCH_RETRY_SECONDS
    _until_held_at(idp, clock.now)
    status, body = _call(idp, door, token, client=client)

    retry = str(int(FETCH_RETRY_SECONDS))
    assert [(answer.status_code, answer.headers.get("retry-after")) for answer in (first, then)] + [status] == [(503, retry), (503, retry), 200], body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_the_issuer_stops_publishing_is_refused_once_the_set_held_is_a_minute_old_and_at_once_after_a_second_read(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """An issuer that restarts with new keys (Dex on memory storage) leaves every token it signed before unverifiable. The
    refresher reads the set again at `KEY_SET_MAX_AGE_SECONDS` from its fetch, so every service drops the key within one
    refresh of the issuer dropping it, whenever each of them last read it. The first request naming the key asks for
    one forced read, which lacks the key too: two reads running without it retire it, so that request is refused 401
    and every later one is refused at once, with no further read of the issuer; a token signed by the new key is let in."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "after-restart")
    clock.now += KEY_SET_MAX_AGE_SECONDS - 1
    assert _call(idp, door, token, client=client)[0] == 200

    clock.now += 1
    _until_held_at(idp, clock.now)
    state = _verifiers[-1]._state[idp.issuer]
    statuses = [_call(idp, door, token, client=client)[0] for _ in range(FORCED_WAITERS + 4)]
    renewed = _call(idp, door, _token(idp, stranger_key, "RS256", kid="after-restart"), client=client)[0]

    assert (statuses, renewed, idp.fetches[JWKS_PATH], state.forced_asked) == ([401] * (FORCED_WAITERS + 4), 200, 3, 1)


@pytest.mark.parametrize("door", ["/gated"])
def test_an_issuer_whose_replicas_serve_different_sets_in_turn_never_has_its_key_refused(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """Behind one address, two replicas of the issuer serve different sets (a rollout under way, a cache per node), and
    the reads land on them in turn: one holds the key the issuer signs with, the other does not. A read without the key
    followed by one with it is no withdrawal, so a token signed by that key is never refused: where the set held lacks
    it, its forced read finds it on the other replica."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    idp.turns[JWKS_PATH] = [_key_set(stranger_key, "other-replica"), idp.documents[JWKS_PATH]]

    statuses: list[int] = []
    for _ in range(6):
        clock.now += KEY_SET_MAX_AGE_SECONDS
        _until_held_at(idp, clock.now)
        statuses += [_call(idp, door, token, client=client)[0] for _ in range(3)]

    assert statuses == [200] * 18


def _publish_and_hold(idp: _IdP, clock: _Clock, key_set: bytes) -> None:
    """The issuer serves ``key_set`` and the refresher's next periodic read holds it."""
    idp.documents[JWKS_PATH] = key_set
    clock.now += KEY_SET_MAX_AGE_SECONDS
    _until_held_at(idp, clock.now)


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_the_issuer_publishes_again_verifies_again_from_the_read_that_finds_it(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """A retired key the issuer publishes again (a rollback of its keys) is trusted again from the read that finds it, and
    is retired no longer: when the issuer drops it once more, one read without it makes it no more than a key the set
    does not hold, so its first request asks for a forced read rather than being refused at once."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    original, rotated = idp.documents[JWKS_PATH], _key_set(stranger_key, "after-restart")
    for _ in range(RETIRE_AFTER_READS):
        _publish_and_hold(idp, clock, rotated)
    retired = _call(idp, door, token, client=client)[0]

    _publish_and_hold(idp, clock, original)
    status, body = _call(idp, door, token, client=client)
    _publish_and_hold(idp, clock, rotated)
    read_before = idp.fetches[JWKS_PATH]
    dropped_again = _call(idp, door, token, client=client)[0]

    assert (retired, status, dropped_again, idp.fetches[JWKS_PATH] - read_before) == (401, 200, 401, 1), body


#: How a test's verifier comes to hold a set without the key `KID` names, in ``(idp, clock, client, rsa_key, stranger_key)``.
_Unseen = Callable[[_IdP, _Clock, TestClient, rsa.RSAPrivateKey, rsa.RSAPrivateKey], None]


def _never_seen(idp: _IdP, clock: _Clock, client: TestClient, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey) -> None:
    """The process first read the set after the issuer had dropped the key: a pod restarted after the issuer's rotation."""
    del clock, rsa_key
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "after-restart")
    assert _call(idp, "/gated", _token(idp, stranger_key, "RS256", kid="after-restart"), client=client)[0] == 200


def _forgotten_past_its_age(idp: _IdP, clock: _Clock, client: TestClient, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey) -> None:
    """The key retired `RETIRED_KID_SECONDS` before the set held was read."""
    assert _call(idp, "/gated", _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    for _ in range(RETIRE_AFTER_READS):
        _publish_and_hold(idp, clock, _key_set(stranger_key, "after-restart"))
    clock.now += RETIRED_KID_SECONDS - KEY_SET_MAX_AGE_SECONDS
    _publish_and_hold(idp, clock, _key_set(stranger_key, "after-restart"))


@pytest.mark.parametrize("unseen", [_never_seen, _forgotten_past_its_age], ids=["never-seen", "past-its-age"])
@pytest.mark.parametrize("door", ["/gated"])
def test_a_dropped_key_this_process_does_not_remember_retiring_costs_one_forced_read_and_is_refused(
    door: str, unseen: _Unseen, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """What the retired memory does not hold meets the rule for a `kid` the set does not hold: one forced read, then 401.
    So the memory's bounds cost a read, never a wrong answer; and a pod that started after the issuer's rotation, which
    never saw the old key, judges it by that rule alone."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    unseen(idp, clock, client, rsa_key, stranger_key)
    state = _verifiers[-1]._state[idp.issuer]
    read_before = idp.fetches[JWKS_PATH]

    status, body = _call(idp, door, token, client=client)

    assert (status, idp.fetches[JWKS_PATH] - read_before, state.forced_asked) == (401, 1, 1), body


@pytest.mark.parametrize("door", ["/gated"])
def test_past_the_cap_the_oldest_retired_key_is_forgotten_and_the_newest_are_refused_at_once(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """The issuer retires `RETIRED_KIDS_PER_ISSUER` keys after the first: the first, the oldest, is forgotten and meets the
    rule for a `kid` the set does not hold (one forced read, then 401); the newest are still refused at once."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    # Each key is retired by the second read that lacks it, so the first key and the next `RETIRED_KIDS_PER_ISSUER` are
    # retired once this many sets have followed it.
    for n in range(RETIRED_KIDS_PER_ISSUER + RETIRE_AFTER_READS):
        _publish_and_hold(idp, clock, _key_set(stranger_key, f"after-{n}"))
    read_before = idp.fetches[JWKS_PATH]

    newest = [_call(idp, door, _token(idp, rsa_key, "RS256", kid=f"after-{n}"), client=client)[0] for n in range(RETIRED_KIDS_PER_ISSUER)]
    read_for_newest = idp.fetches[JWKS_PATH] - read_before
    oldest = _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0]

    assert (newest, read_for_newest, oldest, idp.fetches[JWKS_PATH] - read_before) == ([401] * RETIRED_KIDS_PER_ISSUER, 0, 401, 1)


def test_a_key_one_issuer_retired_is_nothing_to_another(idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """Each issuer's retired keys are its own: a further issuer rotating its key retires it for that issuer's tokens
    alone, and a token of the first issuer naming the same `kid` meets the first issuer's own rule (one forced read of
    it, then 401)."""
    further = Provider()
    try:
        clock = _Clock()
        client = _client(idp, clock=clock, further=OidcProvider(issuer=further.issuer, idp_id="further", allow_insecure=True))
        old = further.mint("ada", audience=AUDIENCE, jkt=BROWSER.jkt)
        assert _call(idp, "/gated", old, client=client)[0] == 200
        further.rotate()
        held = _verifiers[-1]._state
        for _ in range(RETIRE_AFTER_READS):
            clock.now += KEY_SET_MAX_AGE_SECONDS
            _until(lambda: all(getattr(held[issuer].keys, "fetched_at", None) == clock.now for issuer in (idp.issuer, further.issuer)))
        reads = (idp.fetches[JWKS_PATH], further.fetches.count("/jwks"))

        theirs = _call(idp, "/gated", old, client=client)[0]
        ours = _call(idp, "/gated", _token(idp, rsa_key, "RS256", kid=str(jwt.get_unverified_header(old)["kid"])), client=client)[0]
    finally:
        further.close()

    assert (theirs, ours, idp.fetches[JWKS_PATH] - reads[0], further.fetches.count("/jwks") - reads[1]) == (401, 401, 1, 0)


def _burst(idp: _IdP, door: str, token: str, client: TestClient, n: int = 8) -> tuple[list[int], float]:
    """``n`` requests at once, their statuses and the longest any took."""

    def timed(_: int) -> tuple[int, float]:
        began = time.monotonic()
        return _call(idp, door, token, client=client)[0], time.monotonic() - began

    with ThreadPoolExecutor(max_workers=n) as pool:
        answers = list(pool.map(timed, range(n)))
    return [status for status, _ in answers], max(took for _, took in answers)


@pytest.mark.parametrize("door", ["/gated"])
def test_during_an_outage_requests_answer_at_once_from_the_set_held_and_past_its_stale_limit_503_at_once(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarting issuer is not an outage of every service. While the refresher's read hangs, every request is judged
    at once on the last set the issuer served, and none fetches or waits; past `KEY_SET_STALE_LIMIT_SECONDS` from that
    set's fetch, every one is a 503 at once that says when to come back. The issuer holds the read's answer until the
    test has every request's, and the attempt is given longer than that: every request was answered while the read was
    still unanswered and its attempt still running."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", HELD_SECONDS)
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    verifier = _verifiers[-1]
    idp.held.add(JWKS_PATH)
    clock.now = KEY_SET_MAX_AGE_SECONDS
    _until(lambda: idp.fetches[JWKS_PATH] == 2)

    during, _ = _burst(idp, door, token, client)
    clock.now = KEY_SET_STALE_LIMIT_SECONDS
    past, _ = _burst(idp, door, token, client)
    retry_after = client.get(door, headers={"Authorization": f"DPoP {token}", "DPoP": BROWSER.proof("GET", URL, token)}).headers.get("retry-after")
    while_held = (verifier._state[idp.issuer].attempts, idp.fetches[JWKS_PATH], idp.release.is_set())
    idp.release.set()

    assert (during, past, retry_after, while_held) == ([200] * 8, [503] * 8, str(int(FETCH_RETRY_SECONDS)), (1, 2, False))


@pytest.mark.parametrize("door", ["/gated"])
def test_the_refresher_reads_each_document_once_per_interval_however_many_requests_arrive(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """Requests read nothing: past the key set's age, twenty at once cost the issuer one read of it and none of the
    discovery document, which is not due; past the discovery document's age, one read of it and none of the key set."""
    clock = _Clock()
    client = _client(idp, clock=clock, discovery_ttl=int(KEY_SET_MAX_AGE_SECONDS) + 30)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    idp.slow.update({JWKS_PATH, DISCOVERY_PATH})

    reads = []
    for at in (KEY_SET_MAX_AGE_SECONDS, KEY_SET_MAX_AGE_SECONDS + 30):
        clock.now = at
        statuses, _ = _burst(idp, door, token, client, n=20)
        _until(lambda: idp.fetches[JWKS_PATH] + idp.fetches[DISCOVERY_PATH] == 2 + len(reads) + 1)
        _burst(idp, door, token, client, n=20)
        reads.append((statuses, idp.fetches[DISCOVERY_PATH], idp.fetches[JWKS_PATH]))

    assert reads == [([200] * 20, 1, 2), ([200] * 20, 2, 2)]


@pytest.mark.parametrize("door", ["/gated"])
def test_a_discovery_document_that_cannot_be_read_again_is_kept_and_asked_for_again_at_the_pace(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """Discovery is read again every `cache_ttl`; an issuer that cannot answer then keeps the documents read before,
    so its outage is judged by the key set's rule alone rather than refusing every caller at the renewal, and is asked
    again after `FETCH_RETRY_SECONDS`."""
    clock = _Clock()
    client = _client(idp, clock=clock, discovery_ttl=10)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    idp.faults[DISCOVERY_PATH] = _Fault.CLOSED_BEFORE_ANSWERING

    statuses = []
    for at in (11, 12):
        clock.now = at
        _until(lambda: idp.fetches[DISCOVERY_PATH] == 2)
        statuses.append(_call(idp, door, token, client=client)[0])
    time.sleep(0.1)

    assert (statuses, idp.fetches[DISCOVERY_PATH]) == ([200, 200], 2)


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_that_drips_on_one_connection_is_cut_at_the_deadline_and_the_next_read_finds_the_issuer(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issuer that drips a byte at a time on one connection, each byte inside any per-operation timeout (60 s here,
    so nothing but the attempt's deadline can end the read), while it answers new connections as usual: the read is
    cut at the deadline, which the issuer sees as its connection shut down while it still drips, and the reading thread
    ends; the next attempt, on a new connection, reads the issuer, so the set held never ages to its stale limit."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    monkeypatch.setattr(oidc_mod, "HTTP_FETCH_TIMEOUT_SECONDS", 60.0)
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    verifier = _verifiers[-1]
    idp.drip.add(JWKS_PATH)
    clock.now = KEY_SET_MAX_AGE_SECONDS

    _until(lambda: idp.cuts.get(JWKS_PATH, 0) == 1)
    _until(lambda: verifier._state[idp.issuer].attempts == 2 and _fetch_threads() == 0)
    clock.now += FETCH_RETRY_SECONDS
    _until_held_at(idp, clock.now)
    clock.now = KEY_SET_STALE_LIMIT_SECONDS + 1

    status, body = _call(idp, door, token, client=client)

    assert (verifier._state[idp.issuer].attempts >= 3, idp.fetches[JWKS_PATH] >= 3, status) == (True, True, 200), body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_past_the_byte_cap_is_cut_and_never_believed(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """A body past `MAX_DOCUMENT_BYTES` is cut where it passes the cap, whatever it holds: here a sound key set padded
    with whitespace a JSON parser would skip."""
    idp.documents[JWKS_PATH] = _key_set(rsa_key, KID) + b" " * MAX_DOCUMENT_BYTES

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert (status, "could not be reached" in json.loads(body)["detail"]) == (503, True), body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_set_answered_with_a_redirect_is_not_followed(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    """The key set is read from the URL the discovery document named and from nowhere else: a 302 to a sound set on the
    same issuer is an error, and the place it names is never asked."""
    idp.documents["/moved"] = idp.documents[JWKS_PATH]
    idp.redirects[JWKS_PATH] = "/moved"

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert (status, idp.fetches.get("/moved", 0)) == (503, 0), body


@pytest.mark.parametrize("door", ["/gated"])
def test_headers_that_drip_on_two_connections_are_cut_at_the_deadline_and_the_third_attempt_reads_the_issuer(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issuer that answers a status line and then one header line at a time, each well inside any per-operation
    timeout (60 s here, so nothing but the attempt's deadline, enforced from outside the read, can end it), so the
    response never begins: on each of two such connections the issuer sees its connection shut down while it still
    drips, and the reading thread ends, so neither holds a place among `ABANDONED_READS`, and the third attempt reads
    the issuer."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    monkeypatch.setattr(oidc_mod, "HTTP_FETCH_TIMEOUT_SECONDS", 60.0)
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    verifier = _verifiers[-1]
    idp.drip_headers[JWKS_PATH] = 2

    clock.now = KEY_SET_MAX_AGE_SECONDS
    for cut in (1, 2):
        _until(lambda cut=cut: idp.cuts.get(JWKS_PATH, 0) == cut)
        _until(lambda cut=cut: verifier._state[idp.issuer].attempts == cut + 1 and _fetch_threads() == 0)
        clock.now += FETCH_RETRY_SECONDS
    _until_held_at(idp, clock.now)
    status, body = _call(idp, door, token, client=client)

    assert (idp.fetches[JWKS_PATH], status) == (4, 200), body


def test_a_tls_handshake_that_drips_ends_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer that answers the TLS handshake with a record header announcing 16 KiB and then one byte of it at a time,
    each inside any per-operation timeout (60 s here): the read is cut at the attempt's deadline (the handshake's own
    timeout is what was left of it, and the watchdog shuts the connection down there), which the peer sees as its
    connection shut while it still drips, and the reading thread ends."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    monkeypatch.setattr(oidc_mod, "HTTP_FETCH_TIMEOUT_SECONDS", 60.0)
    peer = socket.create_server(("127.0.0.1", 0))
    stopped = threading.Event()
    cut = threading.Event()
    accepted: list[socket.socket] = []

    def drip_handshake() -> None:
        conn = peer.accept()[0]
        accepted.append(conn)
        try:
            conn.recv(4096)
            conn.sendall(b"\x16\x03\x03\x40\x00")
            while not stopped.is_set():
                conn.sendall(b"\x00")
                time.sleep(DRIP_SECONDS)
        except OSError:
            cut.set()

    threading.Thread(target=drip_handshake, daemon=True).start()
    verifier = OIDCVerifier(f"https://127.0.0.1:{peer.getsockname()[1]}", AUDIENCE, cache_ttl=300)
    _verifiers.append(verifier)
    try:
        verifier.start()
        seen = cut.wait(HELD_SECONDS)
        _until(lambda: verifier._state[verifier._issuers[0]].attempts == 1 and _fetch_threads() == 0)
        held = verifier._state[verifier._issuers[0]].keys
    finally:
        stopped.set()
        for conn in accepted:
            conn.close()
        peer.close()

    assert (len(accepted), seen, held) == (1, True, None)


@pytest.mark.parametrize("door", ["/gated"])
def test_a_key_rotated_in_while_a_forced_read_that_began_before_it_is_in_flight_is_never_refused(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """A forged `kid` starts a forced read that the issuer answers slowly, with the set it held when the read began; the
    issuer then rotates a new key in, and a token signed by it joins that read. What the read finds says nothing of a
    key published after it began: the token is told to come back (503), never refused, and is let in by the next read.
    (A token alone whose `kid` a read that started after it asked does not find is refused:
    `test_a_kid_the_key_set_does_not_hold_is_the_callers`.)"""
    clock = _Clock()
    client = _client(idp, clock=clock)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200
    idp.slow.add(JWKS_PATH)
    forged = threading.Thread(target=_call, args=(idp, door, _token(idp, rsa_key, "RS256", kid="forged")), kwargs={"client": client})
    forged.start()
    _until(lambda: idp.fetches[JWKS_PATH] == 2)
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "rotated-in")
    rotated = _token(idp, stranger_key, "RS256", kid="rotated-in")

    joined = client.get(door, headers={"Authorization": f"DPoP {rotated}", "DPoP": BROWSER.proof("GET", URL, rotated)})
    forged.join()
    idp.slow.clear()
    clock.now += UNKNOWN_KID_REFRESH_INTERVAL_SECONDS
    status, body = _call(idp, door, rotated, client=client)

    assert (joined.status_code, joined.headers.get("retry-after") is not None, status) == (503, True, 200), body


@contextmanager
def _lookups_held(monkeypatch: pytest.MonkeyPatch, hosts: set[str], *, first_only: bool = False) -> Generator[threading.Event]:
    """Name lookups of ``hosts`` that the resolver does not answer until the event yielded is set (at the latest, when
    the block ends): the one wait no socket timeout bounds. With ``first_only``, only each host's first lookup is held,
    and later ones resolve to loopback. Other names resolve as usual."""
    resolve = socket.getaddrinfo
    release = threading.Event()
    held: set[str] = set()

    def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host in hosts:
            if not (first_only and host in held):
                held.add(host)
                release.wait()
            return resolve("127.0.0.1", *args, **kwargs)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    try:
        yield release
    finally:
        release.set()


def _fetch_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "oidc-fetch" and t.is_alive())


@pytest.mark.parametrize("door", ["/gated"])
def test_an_attempt_a_hung_lookup_holds_does_not_block_the_next_and_the_threads_it_leaves_are_capped(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name lookup the resolver never answers outlives every socket timeout, so its attempt is abandoned at the
    deadline with its thread still running. The next attempt does not wait for it: when only that lookup hangs, the
    next one reads the issuer. When every lookup hangs, at most `ABANDONED_READS` such threads run for the issuer
    however many attempts are due."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    port = idp.issuer.rsplit(":", 1)[1]
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    verifier = _verifiers[-1]

    with _lookups_held(monkeypatch, {"keys-once.test"}, first_only=True):
        idp.documents[DISCOVERY_PATH] = json.dumps({"issuer": idp.issuer, "jwks_uri": f"http://keys-once.test:{port}{JWKS_PATH}"}).encode()
        clock.now = 400
        _until(lambda: _fetch_threads() == 1 and verifier._state[idp.issuer].attempts >= 2)
        clock.now += FETCH_RETRY_SECONDS
        _until_held_at(idp, clock.now)
        recovered, body = _call(idp, door, token, client=client)
        held_while_recovering = _fetch_threads()
    _until(lambda: _fetch_threads() == 0)

    with _lookups_held(monkeypatch, {"keys-never.test"}):
        idp.documents[DISCOVERY_PATH] = json.dumps({"issuer": idp.issuer, "jwks_uri": f"http://keys-never.test:{port}{JWKS_PATH}"}).encode()
        most = 0
        for _ in range(6):
            attempts = verifier._state[idp.issuer].attempts
            clock.now += 300 + FETCH_RETRY_SECONDS
            _until(lambda attempts=attempts: verifier._state[idp.issuer].attempts > attempts)
            most = max(most, _fetch_threads())

    assert (recovered, held_while_recovering, most) == (200, 1, ABANDONED_READS), body


@pytest.mark.parametrize("door", ["/gated"])
def test_an_abandoned_attempt_that_answers_late_never_replaces_the_set_a_later_attempt_read(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt abandoned at its deadline (its name lookup held, until the test releases it, past it) whose read then
    goes on, once the lookup is answered, to an issuer that now serves another set: the set a later attempt read stays
    the one held, so a late answer cannot bring back a key the issuer had dropped. Two things hold it: the late read is
    cut at its deadline, and an abandoned attempt's outcome is never read."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", CUT_DEADLINE_SECONDS)
    port = idp.issuer.rsplit(":", 1)[1]
    idp.documents[DISCOVERY_PATH] = json.dumps({"issuer": idp.issuer, "jwks_uri": f"http://keys-late.test:{port}{JWKS_PATH}"}).encode()
    clock = _Clock()
    with _lookups_held(monkeypatch, {"keys-late.test"}, first_only=True) as release:
        client = _client(idp, clock=clock)
        verifier = _verifiers[-1]
        verifier.start()
        _until(lambda: verifier._state[idp.issuer].attempts == 1)
        clock.now += FETCH_RETRY_SECONDS
        _until_held_at(idp, clock.now)
        idp.documents[JWKS_PATH] = _key_set(stranger_key, "late")
        release.set()
        _until(lambda: _fetch_threads() == 0)

    held = verifier._state[idp.issuer].keys
    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)

    assert (held is not None and [key.key_id for key in held.keys], status) == ([KID], 200), body


def test_closing_a_verifier_whose_issuers_all_hang_returns_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two issuers whose every read hangs (a lookup no timeout bounds), each attempt given the full deadline (5 s, so
    waiting both out is 10 s, past the 6 s `close` waits for the refresher): closing stops the refresher while it waits
    on the first, so when `close` returns the refresher has ended, with both lookups still held."""
    issuers = ["http://stuck-one.test", "http://stuck-two.test"]
    with _lookups_held(monkeypatch, {"stuck-one.test", "stuck-two.test"}):
        verifier = OIDCVerifier(issuers, AUDIENCE, cache_ttl=300, allow_insecure=True)
        verifier.start()
        _until(lambda: _fetch_threads() == 1)
        verifier.close()
        alive = any(t.name == "oidc-refresher" and t.is_alive() for t in threading.enumerate())

    assert alive is False


@pytest.mark.parametrize("door", ["/gated"])
def test_a_refresher_an_error_ended_is_started_again_by_the_next_request_and_recovers(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing an attempt raises ends the refresher; should it end anyway (here, made to exit), the next request starts
    it again, and it goes on reading: a key the issuer then drops is refused."""
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    assert _call(idp, door, token, client=client)[0] == 200
    verifier = _verifiers[-1]
    due = verifier._due

    def exit_once(configured: str) -> bool:
        monkeypatch.setattr(verifier, "_due", due)
        raise SystemExit

    monkeypatch.setattr(verifier, "_due", exit_once)
    _until(lambda: not any(t.name == "oidc-refresher" and t.is_alive() for t in threading.enumerate()))
    assert _call(idp, door, token, client=client)[0] == 200
    idp.documents[JWKS_PATH] = _key_set(stranger_key, "after-restart")
    clock.now = KEY_SET_MAX_AGE_SECONDS
    _until_held_at(idp, clock.now)

    status, body = _call(idp, door, token, client=client)

    assert status == 401, body


@pytest.mark.parametrize("door", ["/gated"])
def test_a_pod_that_starts_while_its_issuer_is_down_answers_503_at_once_and_recovers_with_no_request_reading(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first request waits for the refresher's first attempt; the requests after it are refused at once and read
    nothing; the refresher's next attempt, once the issuer answers, lets them in. The deadline is `HELD_SECONDS`, so a
    request that waited for an attempt would take longer than half of it, whatever the host's load."""
    monkeypatch.setattr(oidc_mod, "FETCH_DEADLINE_SECONDS", HELD_SECONDS)
    clock = _Clock()
    client = _client(idp, clock=clock)
    token = _token(idp, rsa_key, "RS256")
    idp.faults[JWKS_PATH] = _Fault.CLOSED_BEFORE_ANSWERING
    first = _call(idp, door, token, client=client)[0]
    statuses, took = _burst(idp, door, token, client)
    fetched = idp.fetches[JWKS_PATH]
    del idp.faults[JWKS_PATH]
    clock.now += FETCH_RETRY_SECONDS
    _until_held_at(idp, clock.now)

    status, body = _call(idp, door, token, client=client)

    assert (first, statuses, took < HELD_SECONDS / 2, fetched, status) == (503, [503] * 8, True, 1, 200), body


def test_closing_a_verifier_stops_its_refresher(idp: _IdP) -> None:
    verifier = OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)
    verifier.warm()
    assert any(t.name == "oidc-refresher" and t.is_alive() for t in threading.enumerate())

    verifier.close()

    assert not any(t.name == "oidc-refresher" and t.is_alive() for t in threading.enumerate())


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
    _verifiers.append(OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True))
    cast(FastAPI, app_client.app).state.oidc = PersonVerifier(_verifiers[-1], [unchecked])

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
