"""Who a request is: the person the platform's OIDC provider vouched for, in a token addressed to the workbench.

Any OIDC provider (Keycloak is the reference): the issuer and the workbench's own audience are configured, and the
deployment names the provider's idp id (``WORKBENCH_OIDC_IDP_ID``). A verified token is the principal
``<idp-id>~<sub>`` (`service_kit.governed.principal`), the key every workbench grant names; the idp id keeps two
providers' subjects apart and is never a machine identity's.

The token is DPoP-bound (RFC 9449): its ``cnf.jkt`` names the key the browser holds, and the request's proof must be
signed by that key (`service_kit.governed.dpop`). The verified token is kept on the caller only so a service can
exchange it for a token addressed to the lakehouse (`service_kit.governed.token_exchange`); it is never forwarded.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from service_kit.governed.oidc import IDToken, OIDCVerifier
from service_kit.governed.principal import Principal


class Confirmation(BaseModel):
    """The token's ``cnf`` claim (RFC 7800), as far as DPoP reads it: the thumbprint of the key it is bound to."""

    jkt: str | None = None


class Caller(BaseModel):
    """A person the provider vouched for: their principal, the token's claims, and the token itself for an exchange."""

    model_config = ConfigDict(frozen=True)

    principal: Principal
    claims: IDToken
    access_token: SecretStr

    @property
    def subject(self) -> str:
        """The principal key, the subject every workbench grant and check names."""
        return self.principal.key

    @property
    def jkt(self) -> str | None:
        """The thumbprint of the key the token is bound to, or ``None`` for a token bound to none."""
        try:
            return Confirmation.model_validate((self.claims.model_extra or {}).get("cnf") or {}).jkt
        except ValidationError:
            return None


class PersonVerifier:
    """Verify a token addressed to the workbench, and name the person it proves."""

    def __init__(self, verifier: OIDCVerifier, idp_id: str) -> None:
        self._verifier = verifier
        self._idp_id = idp_id

    def warm(self) -> list[tuple[str, str]]:
        """Resolve the issuer now and report what failed; see `OIDCVerifier.warm`."""
        return self._verifier.warm()

    def verify(self, token: str) -> Caller:
        """The caller ``token`` proves.

        Raises:
            UnauthenticatedError: the token is at fault, or names a subject no principal can carry.
            ProviderUnavailableError: the provider's documents are at fault.
        """
        from lance_namespace import UnauthenticatedError

        claims = self._verifier.verify(token)
        try:
            principal = Principal(idp_id=self._idp_id, subject=claims.sub)
        except ValidationError as exc:
            raise UnauthenticatedError("The token's subject names no principal") from exc
        return Caller(principal=principal, claims=claims, access_token=SecretStr(token))
