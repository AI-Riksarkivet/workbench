"""Who a request is: the person an OIDC provider vouched for, in a token addressed to the workbench.

Any OIDC provider, and several: the deployment names every issuer a person signs in at (`OidcSettings`'s
`oidc_person_providers`: `WORKBENCH_OIDC_ISSUER`, then `WORKBENCH_OIDC_PROVIDERS`), each with an idp id of its own, and a
token is named by the issuer its verified `iss` is. A person is named exactly as the lakehouse names them (rask's ADR
0208, `lakehouse.governed.idp_identity`), so a grant in the workbench's store and a grant in the lakehouse name the same
principal `<idp id>~<subject>`:

- by default the issuer's idp id and the first of its subject claims the token carries, `sub` unless the deployment names
  others (`SubjectNaming`);
- under a Dex connector map, the idp id the token's connector maps to and the upstream user id that connector reports,
  both in Dex's `federated_claims`, which the sign-in client asks for with the `federated:id` scope (`ConnectorNaming`).

A token a client was issued for itself names no person and is refused, where the lakehouse names it a machine: the
workbench's doors serve people, and its services act as themselves on their own client's token. A client's token is one
whose `sub` is one of its own audiences or its `azp`; where the deployment names the issuer's client marker, one carrying
it; otherwise one whose `sub` is the `client_id` it names (RFC 9068 § 2.2), or Dex's encoding of the client its `azp`
names. A public client is never issued one by Dex v2.46.1 (`server/grants/clientcredentials.go` refuses
`unauthorized_client`), and a confidential client's names it in `azp` and `aud`.

The token may be DPoP-bound (RFC 9449): its `cnf.jkt` names the key the browser holds, and the request's proof must then
be signed by that key (`service_kit.governed.dpop`). The verified token is kept on the caller only so a service can
exchange it, or pass it on where the deployment says so (`service_kit.governed.deps`); it is never logged.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence

from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from service_kit.governed.oidc import IDToken, OIDCVerifier
from service_kit.governed.principal import Principal, is_machine_idp
from service_kit.governed.settings import ClientMarker, OidcProvider


log = logging.getLogger(__name__)


class Confirmation(BaseModel):
    """The token's ``cnf`` claim (RFC 7800), as far as DPoP reads it: the thumbprint of the key it is bound to."""

    jkt: str | None = None


class Caller(BaseModel):
    """A person the provider vouched for: their principal, the token's claims, and the token itself."""

    model_config = ConfigDict(frozen=True)

    principal: Principal
    claims: IDToken
    access_token: SecretStr

    @property
    def subject(self) -> str:
        """The principal key, the subject every workbench grant and check names."""
        return self.principal.key

    @property
    def bound(self) -> bool:
        """Whether the token carries a confirmation claim at all: such a token is accepted only with its proof."""
        return "cnf" in (self.claims.model_extra or {})

    @property
    def jkt(self) -> str | None:
        """The thumbprint of the key the token is bound to, or ``None`` for a token bound to none."""
        try:
            return Confirmation.model_validate((self.claims.model_extra or {}).get("cnf") or {}).jkt
        except ValidationError:
            return None

    def addressed_to(self, audience: str) -> bool:
        """Whether the token's `aud` names ``audience``."""
        aud = self.claims.aud
        return audience == aud if isinstance(aud, str) else audience in aud


class FederatedClaims(BaseModel):
    """Dex's `federated_claims`: the connector that authenticated the person, and the id that connector reports."""

    connector_id: str
    user_id: str


class SubjectNaming(BaseModel):
    """A person is the issuer's idp id and the first of `claims` their token carries."""

    model_config = ConfigDict(frozen=True)

    idp_id: str
    claims: tuple[str, ...] = ("sub",)

    def person(self, claims: IDToken) -> Principal:
        carried = claims.model_dump()
        subject = next((value for name in self.claims if isinstance(value := carried.get(name), str) and value), None)
        if subject is None:
            log.warning("oidc_token_names_no_subject", extra={"claims": list(self.claims)})
            raise UnauthenticatedError("The token names no subject this deployment reads")
        return Principal(idp_id=self.idp_id, subject=subject)


class ConnectorNaming(BaseModel):
    """A person is the idp id their Dex connector maps to and the user id that connector reports."""

    model_config = ConfigDict(frozen=True)

    connectors: dict[str, str]

    def person(self, claims: IDToken) -> Principal:
        federated = (claims.model_extra or {}).get("federated_claims")
        if federated is None:
            log.warning("oidc_token_names_no_connector", extra={"reason": "no federated_claims: the sign-in client did not ask for federated:id"})
            raise UnauthenticatedError("The token names no connector: the sign-in client asks for the federated:id scope")
        try:
            connector = FederatedClaims.model_validate(federated)
        except ValidationError as exc:
            raise UnauthenticatedError("Invalid or expired token") from exc
        idp_id = self.connectors.get(connector.connector_id)
        if idp_id is None:
            log.warning("oidc_connector_unmapped", extra={"connector_id": connector.connector_id[:128]})
            raise UnauthenticatedError("The token's connector names no identity provider this deployment knows")
        return Principal(idp_id=idp_id, subject=connector.user_id)


type PersonNaming = SubjectNaming | ConnectorNaming


def person_naming(provider: OidcProvider) -> PersonNaming:
    """The rule `provider` names a person by."""
    if provider.connectors:
        return ConnectorNaming(connectors=provider.connectors)
    if provider.subject_claims is None:
        return SubjectNaming(idp_id=provider.idp_id)
    return SubjectNaming(idp_id=provider.idp_id, claims=provider.subject_claims)


def _dex_client_subject(client_id: str) -> str:
    """Dex's `sub` for a client signed in for itself: base64url, unpadded, of the protobuf `IDTokenSubject{user_id = 1}`
    (dex `server/internal/codec.go`; the empty `conn_id` is a proto3 default, so it is not encoded)."""
    raw = client_id.encode("utf-8")
    length = bytearray()
    size = len(raw)
    while True:
        low, size = size & 0x7F, size >> 7
        length.append(low | (0x80 if size else 0))
        if not size:
            break
    return base64.urlsafe_b64encode(b"\x0a" + bytes(length) + raw).rstrip(b"=").decode("ascii")


def _is_client(claims: IDToken, marker: ClientMarker | None) -> bool:
    """Whether a client was issued the token for itself: by the issuer's marker where named, else by its `sub`. A `sub`
    that is one of the token's own audiences or its authorized party is a client's under any rule: no person's `sub`
    is a client id."""
    extra = claims.model_extra or {}
    client_id, azp = extra.get("client_id"), extra.get("azp")
    audiences = [claims.aud] if isinstance(claims.aud, str) else claims.aud
    if claims.sub in audiences or claims.sub == azp:
        return True
    if marker is not None:
        mark = extra.get(marker.claim)
        return mark is not None and (marker.value is None or mark == marker.value)
    if isinstance(client_id, str) and client_id and claims.sub == client_id:
        return True
    return isinstance(azp, str) and bool(azp) and claims.sub == _dex_client_subject(azp)


class PersonVerifier:
    """Verify a token one of the deployment's issuers addressed to the workbench, and name the person it proves."""

    def __init__(self, verifier: OIDCVerifier, providers: Sequence[OidcProvider]) -> None:
        self._verifier = verifier
        self._providers = {provider.issuer.rstrip("/"): provider for provider in providers}
        self._naming = {issuer: person_naming(provider) for issuer, provider in self._providers.items()}

    def warm(self) -> list[tuple[str, str]]:
        """Resolve every issuer now and report what failed; see `OIDCVerifier.warm`."""
        return self._verifier.warm()

    def verify(self, token: str) -> Caller:
        """The person ``token`` proves.

        Raises:
            UnauthenticatedError: the token is at fault, names no person, or is a client's.
            ProviderUnavailableError: the provider's documents are at fault.
        """
        claims = self._verifier.verify(token)
        issuer = claims.iss.rstrip("/")
        provider = self._providers.get(issuer)
        if provider is None:
            raise UnauthenticatedError("Unrecognized token issuer")
        if _is_client(claims, provider.client_marker):
            log.warning("oidc_client_token_refused", extra={"issuer": issuer})
            raise UnauthenticatedError("The token is a client's, and names no person")
        try:
            principal = self._naming[issuer].person(claims)
        except ValidationError as exc:
            raise UnauthenticatedError("The token's subject names no principal") from exc
        # Settings refuse a machine's idp id for people; a principal that reads as a machine is still never a person.
        if is_machine_idp(principal.idp_id):
            log.warning("oidc_person_named_as_machine", extra={"issuer": issuer})
            raise UnauthenticatedError("The token names a machine, not a person")
        return Caller(principal=principal, claims=claims, access_token=SecretStr(token))
