"""Tokens addressed to the lakehouse: a person's by token exchange (RFC 8693), the workbench's own by client credentials.

A workbench service that reads the lakehouse as a person never forwards the person's token. That token is addressed
to the workbench and bound to the browser's key (RFC 9449 § 7.1 forbids presenting a DPoP-bound token as a bearer),
and a token valid at two audiences could be replayed at either. The service asks the platform's OIDC provider instead
for a short-lived token addressed to the lakehouse, for the same person ("on behalf of"), authenticating as its own
confidential client:

    grant_type=urn:ietf:params:oauth:grant-type:token-exchange
    subject_token=<the person's verified workbench token>
    subject_token_type=urn:ietf:params:oauth:token-type:access_token
    requested_token_type=urn:ietf:params:oauth:token-type:access_token
    audience=<the lakehouse's audience>

The answer is cached per (person, audience) until shortly before it expires, and a cache entry is only ever reached by
the person's own principal key, so a token exchanged for one person is never presented for another. The workbench's own
identity, which writes annotations after the workbench's own role check, takes a client-credentials token for the same
audience, cached the same way.

The client secret is read from its mounted file on every request to the provider, never from the environment, so a
rotated secret is used by the next exchange. The provider's token endpoint is read from its discovery document.

No maintained Python library offers an RFC 8693 client: Authlib 1.9.5's ``authlib.oauth2.rfc8693`` is an empty
package, and httpx-oauth 0.17.0 has no token-exchange grant. The request is a form POST over httpx.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError


log = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a grant type URN, not a secret
ACCESS_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a token type URN, not a secret
#: How long before its expiry a cached token stops being handed out, so a call never starts on a token about to lapse.
EXPIRY_MARGIN_SECONDS: Final = 30.0
#: The most people whose exchanged token one process keeps; past it the least recently used is dropped first.
CACHE_CAPACITY: Final = 1024
HTTP_TIMEOUT_SECONDS: Final = 10.0


class LakehouseCaller(BaseModel):
    """The person a lakehouse read is made as: their principal key, which every per-caller cache is keyed on, and a
    function answering their current token addressed to the lakehouse (``None`` with authentication off)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    subject: str
    token: Callable[[], str] | None = None


class _Discovery(BaseModel):
    token_endpoint: str


class _TokenResponse(BaseModel):
    """RFC 6749 § 5.1 and RFC 8693 § 2.2.1, as far as a caller reads them."""

    model_config = ConfigDict(extra="allow")

    access_token: str = Field(min_length=1)
    token_type: str
    expires_in: int = Field(gt=0)
    issued_token_type: str | None = None


class _Cached(BaseModel):
    token: str
    until: float


class LakehouseTokens:
    """Tokens addressed to ``audience``: each person's by exchange, and this service's own by client credentials.

    Synchronous on purpose: the lakehouse is read through pylance's namespace client, which asks for its headers from
    a worker thread, and an async route reaches this through ``asyncio.to_thread``.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        client_id: str,
        client_secret_file: str,
        discovery_url: str | None = None,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._discovery = f"{(discovery_url or issuer).rstrip('/')}/.well-known/openid-configuration"
        self._audience = audience
        self._client_id = client_id
        self._secret_file = client_secret_file
        self._http = http or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        self._clock = clock
        self._token_endpoint: str | None = None
        self._lock = threading.Lock()
        #: Each person's latest verified workbench token, the subject token of their next exchange.
        self._subject_tokens: dict[str, str] = {}
        self._exchanged: dict[str, _Cached] = {}
        self._own: _Cached | None = None

    @classmethod
    def from_settings(cls, settings: object) -> LakehouseTokens | None:
        """The client a service's `LakehouseTokenSettings` and `OidcSettings` name, or ``None`` when they name none."""
        issuer = getattr(settings, "oidc_issuer", None)
        audience = getattr(settings, "lakehouse_audience", None)
        client_id = getattr(settings, "oidc_client_id", None)
        secret_file = getattr(settings, "oidc_client_secret_file", None)
        if not (getattr(settings, "oidc_enabled", False) and issuer and audience and client_id and secret_file):
            return None
        return cls(
            issuer=issuer, audience=audience, client_id=client_id, client_secret_file=secret_file, discovery_url=getattr(settings, "oidc_discovery_url", None)
        )

    def remember(self, subject: str, workbench_token: str) -> None:
        """Keep ``workbench_token`` as ``subject``'s subject token for an exchange: called with each verified request."""
        with self._lock:
            self._subject_tokens.pop(subject, None)
            self._subject_tokens[subject] = workbench_token
            while len(self._subject_tokens) > CACHE_CAPACITY:
                dropped = next(iter(self._subject_tokens))
                del self._subject_tokens[dropped]
                self._exchanged.pop(dropped, None)

    def for_person(self, subject: str) -> str:
        """A token addressed to the lakehouse for ``subject``: the cached one, or a fresh exchange of their latest token.

        Raises:
            UnauthorizedError: no verified request from ``subject`` left a token to exchange, or the provider refused it.
            ServiceUnavailableError: the provider could not be asked, or answered something that is not a token.
        """
        with self._lock:
            cached = self._exchanged.get(subject)
            if cached is not None and cached.until > self._clock():
                return cached.token
            subject_token = self._subject_tokens.get(subject)
        if subject_token is None:
            raise UnauthorizedError("no verified workbench token to exchange for this person")
        fresh = self._ask(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": subject_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "audience": self._audience,
            },
            exchange=True,
        )
        with self._lock:
            if subject in self._subject_tokens:
                self._exchanged[subject] = fresh
        return fresh.token

    def for_service(self) -> str:
        """This service's own token addressed to the lakehouse (client credentials), cached until shortly before expiry."""
        with self._lock:
            if self._own is not None and self._own.until > self._clock():
                return self._own.token
        fresh = self._ask({"grant_type": "client_credentials", "audience": self._audience}, exchange=False)
        with self._lock:
            self._own = fresh
        return fresh.token

    def close(self) -> None:
        self._http.close()

    def _endpoint(self) -> str:
        if self._token_endpoint is None:
            try:
                answer = self._http.get(self._discovery)
                answer.raise_for_status()
                self._token_endpoint = _Discovery.model_validate_json(answer.content).token_endpoint
            except (httpx.HTTPError, ValidationError) as exc:
                raise ServiceUnavailableError("the identity provider's discovery document could not be read") from exc
        return self._token_endpoint

    def _secret(self) -> str:
        try:
            secret = Path(self._secret_file).read_text().strip()
        except OSError as exc:
            raise ServiceUnavailableError(f"the client secret at {self._secret_file} could not be read") from exc
        if not secret:
            raise ServiceUnavailableError(f"the client secret at {self._secret_file} is empty")
        return secret

    def _ask(self, form: dict[str, str], *, exchange: bool) -> _Cached:
        grant = "token_exchange" if exchange else "client_credentials"
        try:
            answer = self._http.post(self._endpoint(), data=form, auth=(self._client_id, self._secret()))
        except httpx.HTTPError as exc:
            log.error("token_request_unreachable", extra={"grant": grant})
            raise ServiceUnavailableError("the identity provider could not be reached") from exc
        error = _error_code(answer)
        if exchange and answer.status_code == 400 and error == "invalid_grant":
            # RFC 6749 § 5.2: `invalid_grant` is the one error about the grant itself, here the person's subject token
            # (expired, revoked, not exchangeable): the person signs in again. Its description is never logged, as it may
            # echo the token.
            log.warning("token_exchange_refused", extra={"status": answer.status_code, "error": error})
            raise UnauthorizedError("the identity provider refused to exchange the caller's token")
        if answer.status_code != 200:
            # Every other refusal (`invalid_client`, `unauthorized_client`, `unsupported_grant_type`, `invalid_request`,
            # `invalid_scope`, RFC 8693's `invalid_target`, a 401) is this service's client or its configuration: an
            # outage of the service, never the person's fault.
            log.error("token_request_failed", extra={"grant": grant, "status": answer.status_code, "error": error})
            raise ServiceUnavailableError("the identity provider did not issue a token")
        try:
            issued = _TokenResponse.model_validate_json(answer.content)
        except ValidationError as exc:
            raise ServiceUnavailableError("the identity provider answered with no token") from exc
        if issued.token_type.lower() != "bearer" or (exchange and issued.issued_token_type not in (None, ACCESS_TOKEN_TYPE)):
            log.error("token_request_unusable", extra={"grant": grant, "token_type": issued.token_type, "issued_token_type": issued.issued_token_type})
            raise ServiceUnavailableError("the identity provider issued a token this service cannot present")
        return _Cached(token=issued.access_token, until=self._clock() + issued.expires_in - EXPIRY_MARGIN_SECONDS)


def _error_code(answer: httpx.Response) -> str:
    try:
        value = answer.json().get("error")
    except ValueError:
        return ""
    return value if isinstance(value, str) else ""
