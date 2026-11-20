"""Tokens addressed to the lakehouse: a person's by token exchange (RFC 8693), the workbench's own by client credentials.

Where the provider exchanges its own tokens for another audience (`LakehouseDelegation.EXCHANGE`), a workbench service
that reads the lakehouse as a person never forwards the person's token. That token is addressed to the workbench and may
be bound to the browser's key (RFC 9449 § 7.1 forbids presenting a DPoP-bound token as a bearer), and a token valid at
two audiences could be replayed at either. The service asks the provider instead for a short-lived token addressed to the
lakehouse, for the same person ("on behalf of"), authenticating as its own confidential client:

    grant_type=urn:ietf:params:oauth:grant-type:token-exchange
    subject_token=<the person's verified workbench token>
    subject_token_type=urn:ietf:params:oauth:token-type:access_token
    requested_token_type=urn:ietf:params:oauth:token-type:access_token
    audience=<the lakehouse's audience>

Where the provider exchanges no such token (Dex), the deployment passes the person's own token on instead
(`LakehouseDelegation.FORWARD`, `service_kit.governed.deps`). Either way the token a read presents is the person's
LATEST verified one (`PersonTokens`), and none outlives its own `exp`. A token is exchanged only at the issuer that
signed it, by the client the deployment names there (`OidcProvider.lakehouse`), so no other provider is ever handed it.

The answer is cached per person and subject token until shortly before it expires, and never past the subject token's
own `exp`; a cache entry is only ever reached by the person's own principal key, so a token exchanged for one person is
never presented for another. The workbench's own
identity, which writes annotations after the workbench's own role check, takes a client-credentials token for the same
audience, cached the same way; it also asks for the audience's scope where the provider reads the audience from one
(Dex's `audience:server:client_id:<audience>`, which the audience client issues only to a client it trusts).

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
from service_kit.governed.dpop import challenge
from service_kit.governed.settings import LakehouseDelegation


log = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a grant type URN, not a secret
ACCESS_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a token type URN, not a secret
#: How long before its expiry a cached token stops being handed out, so a call never starts on a token about to lapse.
EXPIRY_MARGIN_SECONDS: Final = 30.0
#: The challenge a refusal of the person's own session carries (no unexpired token of theirs, an exchange of it
#: refused): the session must be renewed, which the app asks the person to do, as for the door's own refusal.
SESSION_TO_RENEW: Final = challenge("invalid_token", on="dpop", bearer=True, description="the session must be renewed")
#: The most people whose exchanged token one process keeps; past it the least recently used is dropped first.
CACHE_CAPACITY: Final = 1024
HTTP_TIMEOUT_SECONDS: Final = 10.0


class LakehouseCaller(BaseModel):
    """The person a lakehouse read is made as: their principal key, which every per-caller cache is keyed on, a function
    answering their current token addressed to the lakehouse (``None`` with authentication off), one answering the
    workbench's own, for a source the deployment reads as the workbench (``None`` where no client is configured), and the
    names of the sources they may read as the workbench's own store answered (every source it does not decide among them;
    `client_kit.media.source_gate.admit`). A caller nobody admitted reads no source the workbench decides for
    (`client_kit.sources.require_admitted`) and lists none."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    subject: str
    token: Callable[[], str] | None = None
    service: Callable[[], str] | None = None
    admitted: frozenset[str] = frozenset()


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


class _Held(BaseModel):
    """A person's latest verified token, the issuer that signed it, and its own `exp` (epoch seconds)."""

    token: str
    issuer: str
    expires_at: float


class PersonTokens:
    """Each person's latest verified token, kept no longer than its own `exp`.

    A dataset handle a service caches for a person (`client_kit.lancekit.registry`) is built once and asks for the
    person's token on every catalog call, so the token a call presents is the latest one the person proved, never the
    one the handle was built on. A token is dropped when it expires, read or not: every write sweeps out the expired
    ones, and past `CACHE_CAPACITY` people the least recently seen is dropped first.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._held: dict[str, _Held] = {}

    def now(self) -> float:
        return self._clock()

    def remember(self, subject: str, *, issuer: str, token: str, expires_at: float) -> None:
        """Keep ``token``, which ``issuer`` signed and which expires at ``expires_at``, as ``subject``'s latest."""
        with self._lock:
            self._held.pop(subject, None)
            self._held[subject] = _Held(token=token, issuer=issuer.rstrip("/"), expires_at=expires_at)
            now = self._clock()
            for expired in [key for key, held in self._held.items() if held.expires_at <= now]:
                del self._held[expired]
            while len(self._held) > CACHE_CAPACITY:
                del self._held[next(iter(self._held))]

    def __len__(self) -> int:
        """How many people's tokens are held."""
        with self._lock:
            return len(self._held)

    def latest(self, subject: str, *, issuer: str) -> _Held:
        """``subject``'s latest token from ``issuer``, while it has not expired.

        Raises:
            UnauthorizedError: no verified request from ``subject`` left one, or it expired: the person signs in again.
        """
        with self._lock:
            held = self._held.get(subject)
            if held is not None and held.expires_at <= self._clock():
                del self._held[subject]
                held = None
        if held is None or held.issuer != issuer.rstrip("/"):
            raise UnauthorizedError("no unexpired token of this person's to present to the lakehouse", headers=SESSION_TO_RENEW)
        return held


class LakehouseTokens:
    """Tokens addressed to ``audience``: each person's by exchange at ``issuer``, the issuer that signed their token, and
    this service's own by client credentials.

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
        people: PersonTokens | None = None,
        audience_scope: str | None = None,
        discovery_url: str | None = None,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._fetch_base = (discovery_url or issuer).rstrip("/")
        self._discovery = f"{self._fetch_base}/.well-known/openid-configuration"
        self._audience = audience
        self._audience_scope = audience_scope
        self._client_id = client_id
        self._secret_file = client_secret_file
        self._http = http or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        self._clock = clock
        self._token_endpoint: str | None = None
        self._lock = threading.Lock()
        self.people = people or PersonTokens()
        #: Each person's exchanged token, keyed by the subject token it was exchanged for: a person who renews their
        #: sign-in is exchanged again, and no entry outlives the subject token it came from.
        self._exchanged: dict[str, tuple[str, _Cached]] = {}
        self._own: _Cached | None = None

    def remember(self, subject: str, token: str, expires_at: float) -> None:
        """Keep ``token``, which this issuer signed for ``subject``, as the subject token of their next exchange."""
        self.people.remember(subject, issuer=self._issuer, token=token, expires_at=expires_at)
        with self._lock:
            self._sweep()

    def _sweep(self) -> None:
        """Drop every exchanged token past the time it is handed out until; called under the lock."""
        now = self._clock()
        for expired in [key for key, (_, cached) in self._exchanged.items() if cached.until <= now]:
            del self._exchanged[expired]

    def for_person(self, subject: str) -> str:
        """A token addressed to the lakehouse for ``subject``: the cached exchange of their latest token, or a fresh one.

        Raises:
            UnauthorizedError: no verified request from ``subject`` left an unexpired token to exchange, or the provider
                refused it.
            ServiceUnavailableError: the provider could not be asked, or answered something that is not a token.
        """
        held = self.people.latest(subject, issuer=self._issuer)
        with self._lock:
            self._sweep()
            hit = self._exchanged.get(subject)
            if hit is not None and hit[0] == held.token:
                return hit[1].token
        fresh = self._ask(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": held.token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "audience": self._audience,
            },
            exchange=True,
        )
        # The exchanged token is handed out no longer than the token it was exchanged for lives.
        until = min(fresh.until, self._clock() + held.expires_at - self.people.now())
        with self._lock:
            self._exchanged.pop(subject, None)
            self._exchanged[subject] = (held.token, _Cached(token=fresh.token, until=until))
            while len(self._exchanged) > CACHE_CAPACITY:
                del self._exchanged[next(iter(self._exchanged))]
        return fresh.token

    def for_service(self) -> str:
        """This service's own token addressed to the lakehouse (client credentials), cached until shortly before expiry."""
        with self._lock:
            if self._own is not None and self._own.until > self._clock():
                return self._own.token
        form = {"grant_type": "client_credentials", "audience": self._audience}
        if self._audience_scope:
            form["scope"] = f"openid {self._audience_scope}"
        fresh = self._ask(form, exchange=False)
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
                endpoint = _Discovery.model_validate_json(answer.content).token_endpoint
            except (httpx.HTTPError, ValidationError) as exc:
                raise ServiceUnavailableError("the identity provider's discovery document could not be read") from exc
            # Split horizon, as the verifier's key set (`oidc.OIDCVerifier`): a provider reached in-cluster at another
            # base than the public issuer its documents name is asked at that base, its token endpoint's path kept.
            if self._fetch_base != self._issuer and (endpoint == self._issuer or endpoint.startswith(f"{self._issuer}/")):
                endpoint = self._fetch_base + endpoint[len(self._issuer) :]
            self._token_endpoint = endpoint
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
            raise UnauthorizedError("the identity provider refused to exchange the caller's token", headers=SESSION_TO_RENEW)
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


class Delegation(BaseModel):
    """How one issuer's people's lakehouse reads are made: exchanged by ``tokens``, that issuer's own exchange client, or
    their own token passed on (``tokens`` is then unset)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    mode: LakehouseDelegation
    tokens: LakehouseTokens | None = None


def _error_code(answer: httpx.Response) -> str:
    try:
        value = answer.json().get("error")
    except ValueError:
        return ""
    return value if isinstance(value, str) else ""
