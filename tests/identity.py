"""A real-socket OIDC provider and a browser's DPoP key, for the offline suites that sign a person in.

`Provider` serves discovery, its key set and a token endpoint on a loopback port, so the services' own verifier and
token client reach it over HTTP as they reach Keycloak. Its token endpoint holds the RFC 8693 request to the rules a
provider applies: the grant type, a subject token it issued and can verify, its type, the audience asked for, and the
confidential client's authentication; a client-credentials request is answered for the client itself. Each request it
answered is kept in `requests`.

`BrowserKey` is the key a browser binds its token to (RFC 9449): its thumbprint goes in the token's `cnf.jkt`, and it
signs one proof per request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jws
from joserfc.jwk import ECKey
from jwt.algorithms import RSAAlgorithm


TOKEN_EXCHANGE: Final = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN: Final = "urn:ietf:params:oauth:token-type:access_token"
WORKBENCH_AUDIENCE: Final = "workbench"
LAKEHOUSE_AUDIENCE: Final = "lakehouse"
CLIENT_ID: Final = "workbench-services"
CLIENT_SECRET: Final = "client-secret-for-tests"
IDP_ID: Final = "keycloak"


class BrowserKey:
    """An EC P-256 key a browser holds, and the proofs it signs with it."""

    def __init__(self) -> None:
        self._key = ECKey.generate_key("P-256")

    @property
    def jkt(self) -> str:
        return self._key.thumbprint()

    def proof(self, method: str, url: str, access_token: str | None, *, header: Mapping[str, Any] | None = None, **claims: Any) -> str:
        """One proof for ``method`` and ``url``, carrying ``ath`` for ``access_token``; ``header`` and ``claims`` override."""
        body: dict[str, Any] = {"jti": uuid.uuid4().hex, "htm": method, "htu": url, "iat": int(time.time())}
        if access_token is not None:
            body["ath"] = base64.urlsafe_b64encode(hashlib.sha256(access_token.encode()).digest()).rstrip(b"=").decode()
        body.update(claims)
        head = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._key.as_dict(private=False)} | dict(header or {})
        return jws.serialize_compact(head, json.dumps({k: v for k, v in body.items() if v is not None}).encode(), self._key, algorithms=[str(head["alg"])])


class Provider:
    """The OIDC provider on a loopback port; see the module docstring."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = uuid.uuid4().hex[:8]
        self.requests: list[dict[str, str]] = []
        self.refuse_exchange = False
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _answer(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    self._answer(
                        200,
                        {
                            "issuer": provider.issuer,
                            "jwks_uri": f"{provider.issuer}/jwks",
                            "token_endpoint": f"{provider.issuer}/token",
                            "id_token_signing_alg_values_supported": ["RS256"],
                        },
                    )
                elif self.path == "/jwks":
                    public = RSAAlgorithm.to_jwk(provider._key.public_key(), as_dict=True)
                    self._answer(200, {"keys": [public | {"kid": provider._kid, "use": "sig", "alg": "RS256"}]})
                else:
                    self._answer(404, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                provider.requests.append(form | {"authorization": self.headers.get("authorization") or ""})
                status, body = provider._token(form, self.headers.get("authorization") or "")
                self._answer(status, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.issuer = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def mint(self, sub: str, *, audience: str = WORKBENCH_AUDIENCE, jkt: str | None = None, lifetime: int = 300, **claims: Any) -> str:
        """A token this provider signs for ``sub`` at ``audience``, bound to ``jkt`` when given."""
        now = int(time.time())
        body: dict[str, Any] = {"iss": self.issuer, "sub": sub, "aud": audience, "iat": now, "exp": now + lifetime} | claims
        if jkt is not None:
            body["cnf"] = {"jkt": jkt}
        return jwt.encode(body, self._key, algorithm="RS256", headers={"kid": self._kid})

    def _token(self, form: Mapping[str, str], authorization: str) -> tuple[int, dict[str, Any]]:
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if authorization != expected:
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") == "client_credentials":
            return 200, self._issued(self.mint(f"service-account-{CLIENT_ID}", audience=form.get("audience") or LAKEHOUSE_AUDIENCE))
        if form.get("grant_type") != TOKEN_EXCHANGE:
            return 400, {"error": "unsupported_grant_type"}
        if form.get("subject_token_type") != ACCESS_TOKEN or form.get("requested_token_type") not in (None, ACCESS_TOKEN):
            return 400, {"error": "invalid_request"}
        if form.get("audience") != LAKEHOUSE_AUDIENCE or self.refuse_exchange:
            return 400, {"error": "invalid_target" if not self.refuse_exchange else "invalid_grant"}
        try:
            subject = jwt.decode(form.get("subject_token") or "", self._key.public_key(), algorithms=["RS256"], audience=WORKBENCH_AUDIENCE)
        except jwt.PyJWTError:
            return 400, {"error": "invalid_grant"}
        return 200, self._issued(self.mint(subject["sub"], audience=LAKEHOUSE_AUDIENCE)) | {"issued_token_type": ACCESS_TOKEN}

    @staticmethod
    def _issued(token: str) -> dict[str, Any]:
        return {"access_token": token, "token_type": "Bearer", "expires_in": 300}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
