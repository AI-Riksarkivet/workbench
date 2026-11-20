"""The workbench's sign-in and authorization settings, one ``WORKBENCH_*`` name each.

Three mixins, because the services have three needs:

* `OidcSettings`: verify a person's token, addressed to the workbench's own audience, from any OIDC provider, and its
  DPoP proof: required unless the deployment says its provider binds no token (`WORKBENCH_DPOP_REQUIRED`).
* `FgaSettings`: the workbench's own OpenFGA store, found by its name, and the model this image carries in it.
* `LakehouseTokenSettings`: how a person's read reaches the lakehouse (exchanged at a provider that serves RFC 8693 for
  its tokens, or the person's own token passed on where it already names the lakehouse's audience), and the workbench's
  own confidential client, with which a service exchanges and takes its own token for the writes it makes as itself.

`GovernedAuthSettings` composes the first two with their coupling: authorization asks about a person, so it needs one
authenticated. Every credential is a mounted file (``*_FILE``), never a value in the environment.

Mix into a ``pydantic-settings`` ``BaseSettings`` subclass; they are plain mixins so the service's own class keeps its
``model_config``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from service_kit.config import StringList
from service_kit.governed.dpop import DEFAULT_WINDOW_SECONDS
from service_kit.governed.fga import DEFAULT_STORE_NAME
from service_kit.governed.principal import CLIENT_IDP_SUFFIX, is_machine_idp, require_idp_id


#: The claims every verified token carries: a client marker on one of them would mark every token a client's.
_VERIFIED_CLAIMS = frozenset({"iss", "sub", "aud", "exp", "iat"})


def _require_a_persons_idp_id(what: str, idp_id: str) -> None:
    require_idp_id(idp_id)
    if is_machine_idp(idp_id):
        raise ValueError(f"{what} {idp_id!r}, a machine identity's idp id")


class ClientMarker(BaseModel):
    """How an issuer marks a token its client was issued for itself, where the token's `sub` is not the client's id: a
    claim, and the value it must hold (any, unset). Keycloak's service-account tokens carry `client_id`; Entra's app-only
    tokens carry `idtyp: app`. The lakehouse reads the same marker (`docs/rask` ADR 0208)."""

    model_config = ConfigDict(frozen=True)

    claim: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def _marks_something(self) -> Self:
        if self.claim in _VERIFIED_CLAIMS:
            raise ValueError(f"a client marker on {self.claim!r}, which every verified token carries, would mark every token a client's")
        return self


class LakehouseDelegation(StrEnum):
    """How a person's read reaches the lakehouse.

    `exchange`: the service exchanges the person's token at the provider for one addressed to the lakehouse (RFC 8693),
    which needs a provider that exchanges ITS OWN tokens for another audience (Keycloak's standard token exchange).
    `forward`: the person's own token, which the sign-in client asked for the lakehouse's audience as well, is passed on
    unchanged. Configured rather than read from discovery: Dex advertises the token-exchange grant (measured on the dev
    Dex v2.46.1, 2026-11-16) but exchanges only an upstream connector's tokens, never its own for another audience.
    """

    EXCHANGE = "exchange"
    FORWARD = "forward"


class ProviderDelegation(BaseModel):
    """How one issuer's people's reads reach the lakehouse: exchanged at that issuer, with the workbench's client there
    (its id and the mounted file holding its secret), or their own token passed on. A token is only ever exchanged at the
    issuer that signed it, so no other provider is handed it."""

    model_config = ConfigDict(frozen=True)

    delegation: LakehouseDelegation
    client_id: str | None = None
    client_secret_file: str | None = None
    #: The scope the client asks for beside the `audience` parameter (Dex's `audience:server:client_id:<audience>`).
    audience_scope: str | None = None

    @model_validator(mode="after")
    def _a_client_to_exchange_with(self) -> Self:
        if self.delegation is LakehouseDelegation.EXCHANGE and not (self.client_id and self.client_secret_file):
            raise ValueError("an exchange is made by the workbench's client at the issuer: name its client_id and client_secret_file")
        return self


class OidcProvider(BaseModel):
    """One issuer a person signs in at, and the rule its people are named by (`service_kit.governed.identity`): the rule
    the lakehouse names them by, so a grant in the workbench's store and one in the lakehouse name the same person."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(min_length=1)
    idp_id: str
    #: Split-horizon fetch: discovery and keys fetched here while tokens keep `issuer`.
    discovery_url: str | None = None
    #: The claims a person's subject is read from, the first the token carries winning; unset is `sub` alone.
    subject_claims: tuple[str, ...] | None = None
    #: The idp id each Dex connector's people are principals of, keyed by the connector id Dex reports in
    #: `federated_claims` (the sign-in client asks for the `federated:id` scope). Empty unless the issuer is such a Dex.
    connectors: dict[str, str] = Field(default_factory=dict)
    client_marker: ClientMarker | None = None
    #: Whether this issuer may be fetched over http: declared per issuer, for a development provider only.
    allow_insecure: bool = False
    #: How this issuer's people's lakehouse reads are made; unset, a read of a lakehouse that authenticates is refused.
    lakehouse: ProviderDelegation | None = None

    @model_validator(mode="after")
    def _one_rule(self) -> Self:
        if self.subject_claims is not None:
            if self.connectors:
                raise ValueError("subject claims are never read beside a Dex connector map, which reports its user id in federated_claims")
            if not self.subject_claims or not all(self.subject_claims):
                raise ValueError("the subject claims name no claim, or an empty one")
        if "" in self.connectors:
            raise ValueError("the connector map names an empty connector id")
        _require_a_persons_idp_id("the idp id is", self.idp_id)
        for connector, idp_id in self.connectors.items():
            _require_a_persons_idp_id(f"the connector map maps {connector!r} to", idp_id)
        return self

    @property
    def idp_ids(self) -> frozenset[str]:
        """Every idp id this issuer's people and clients are named under, which no other issuer may name."""
        return frozenset({self.idp_id, f"{self.idp_id}{CLIENT_IDP_SUFFIX}", *self.connectors.values()})


class FgaSettings:
    """The workbench's own OpenFGA store."""

    fga_enabled: bool = Field(default=False, alias="WORKBENCH_FGA_ENABLED")
    fga_api_url: str = Field(default="http://openfga:8080", alias="WORKBENCH_FGA_API_URL")
    #: The store's name; the model writer creates it, and a service finds it by this name.
    fga_store: str = Field(default=DEFAULT_STORE_NAME, min_length=1, alias="WORKBENCH_FGA_STORE")
    #: A model id to pin. Unset, a service checks against the model its own image carries, found by its body.
    fga_model_id: str | None = Field(default=None, alias="WORKBENCH_FGA_MODEL_ID")
    #: The mounted secret file holding the store's preshared key, read on every request. Unset sends no credential,
    #: which only an OpenFGA authenticating nobody admits.
    fga_credential_file: str | None = Field(default=None, alias="WORKBENCH_FGA_CREDENTIAL_FILE")


class OidcSettings:
    """Verify a person's token addressed to the workbench, and its DPoP proof where the provider binds tokens."""

    #: ON by default: a service authenticates every request unless an operator says, loudly, that it must not.
    oidc_enabled: bool = Field(default=True, alias="WORKBENCH_OIDC_ENABLED")
    #: The one way to serve unauthenticated, for a local development stack only: every route then answers `anon` and
    #: every check allows. Logged at startup; refused at construction unless OIDC is off too, and never set in production.
    insecure_allow_unauthenticated: bool = Field(default=False, alias="WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED")
    oidc_issuer: str | None = Field(default=None, alias="WORKBENCH_OIDC_ISSUER")
    #: The workbench's own audience: a token addressed to anything else (the lakehouse's among them) is refused.
    oidc_audience: str | None = Field(default=None, alias="WORKBENCH_OIDC_AUDIENCE")
    #: Where discovery and the key set are fetched from when it differs from the issuer (a reverse-proxied provider).
    oidc_discovery_url: str | None = Field(default=None, alias="WORKBENCH_OIDC_DISCOVERY_URL")
    oidc_cache_ttl: int = Field(default=3600, alias="WORKBENCH_OIDC_CACHE_TTL")
    oidc_leeway: int = Field(default=60, alias="WORKBENCH_OIDC_LEEWAY")
    oidc_allow_insecure: bool = Field(default=False, alias="WORKBENCH_OIDC_ALLOW_INSECURE")
    #: The idp id of the issuer's people: `corp` makes the person the issuer names `s` the principal `corp~s`. Under
    #: `oidc_connectors` it names no person, and the connectors' idp ids do.
    oidc_idp_id: str | None = Field(default=None, alias="WORKBENCH_OIDC_IDP_ID")
    #: The claims a person's subject is read from, the first the token carries winning; unset is `sub` alone, which
    #: OpenID Connect Core 1.0 § 2 makes unique and never reassigned within the issuer. Never read beside `oidc_connectors`.
    oidc_subject_claims: StringList | None = Field(default=None, alias="WORKBENCH_OIDC_SUBJECT_CLAIMS")
    #: The idp id each Dex connector's people are principals of (`{"local": "dex"}`), as the lakehouse names them.
    oidc_connectors: dict[str, str] = Field(default_factory=dict, alias="WORKBENCH_OIDC_CONNECTORS")
    #: How the issuer marks a token its client was issued for itself, which names no person and is refused.
    oidc_client_marker: ClientMarker | None = Field(default=None, alias="WORKBENCH_OIDC_CLIENT_MARKER")
    #: Further issuers a person signs in at beside `oidc_issuer`, each with its own idp id and naming rule.
    oidc_providers: list[OidcProvider] = Field(default_factory=list, alias="WORKBENCH_OIDC_PROVIDERS")
    #: Whether every token must be presented with a DPoP proof (RFC 9449). ON by default; off only where the provider
    #: binds no token (its discovery advertises no `dpop_signing_alg_values_supported`), and then a token presented as a
    #: bearer is accepted only when it carries no `cnf` claim: a bound token always needs its proof.
    dpop_required: bool = Field(default=True, alias="WORKBENCH_DPOP_REQUIRED")
    #: How far a DPoP proof's `iat` may lie from now, and how long its `jti` is remembered against a replay.
    dpop_window_seconds: int = Field(default=DEFAULT_WINDOW_SECONDS, ge=1, le=600, alias="WORKBENCH_DPOP_WINDOW_SECONDS")
    #: The URL the browser addresses this service by, which a proof's `htu` names: the public origin, or the public prefix
    #: a gateway rewrites to `public_mount`. Unset, the request's own URL: right when nothing between the browser and
    #: the service rewrites the scheme, host or path.
    public_url: str | None = Field(default=None, alias="WORKBENCH_PUBLIC_URL")
    #: The path a gateway rewrites `public_url`'s prefix to before the request reaches this service; empty for none.
    public_mount: str = Field(default="", alias="WORKBENCH_PUBLIC_MOUNT")

    @model_validator(mode="after")
    def _validate_oidc(self) -> Self:
        """A misconfiguration knowable at construction fails here, not as a 401 on every valid request."""
        if not self.oidc_enabled:
            if not self.insecure_allow_unauthenticated:
                raise ValueError(
                    "WORKBENCH_OIDC_ENABLED is off and WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED is not set: this service would serve "
                    "every route to anyone. Turn OIDC on, or set WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED=true for a local stack."
                )
            return self
        if self.insecure_allow_unauthenticated:
            raise ValueError("WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED contradicts WORKBENCH_OIDC_ENABLED; set one")
        if not (self.oidc_issuer and self.oidc_audience and self.oidc_idp_id):
            raise ValueError("WORKBENCH_OIDC_ISSUER, WORKBENCH_OIDC_AUDIENCE and WORKBENCH_OIDC_IDP_ID are required when OIDC is enabled")
        claimed: dict[str, str] = {}
        issuers: set[str] = set()
        for provider in self.oidc_person_providers:
            issuer = provider.issuer.rstrip("/")
            if issuer in issuers:
                raise ValueError(f"{issuer} is named as an issuer twice: one token would be named by two rules")
            issuers.add(issuer)
            for idp_id in provider.idp_ids:
                if claimed.setdefault(idp_id, issuer) != issuer:
                    raise ValueError(f"the idp id {idp_id!r} is named by {claimed[idp_id]} and {issuer}: their people would share one namespace")
            if not provider.allow_insecure and not issuer.startswith("https://"):
                raise ValueError(f"the issuer {issuer} must use HTTPS (declare it insecure-allowed for a dev provider)")
            self._validate_delegation(issuer, provider.lakehouse)
        return self

    def _validate_delegation(self, issuer: str, lakehouse: ProviderDelegation | None) -> None:
        if lakehouse is None or lakehouse.delegation is not LakehouseDelegation.FORWARD:
            return
        # RFC 9449 § 7.1: a DPoP-bound token is never presented as a bearer, so a deployment whose provider binds every
        # token cannot pass one on; it exchanges it.
        if self.dpop_required:
            raise ValueError(
                f"{issuer}'s people's tokens are passed on (forward), which a DPoP-bound token never is: "
                "exchange them, or set WORKBENCH_DPOP_REQUIRED=false for a provider that binds no token"
            )
        if not getattr(self, "lakehouse_audience", None):
            raise ValueError("passing a token on (forward) needs WORKBENCH_LAKEHOUSE_AUDIENCE, which a passed-on token must name")

    @property
    def oidc_person_providers(self) -> list[OidcProvider]:
        """Every issuer a person signs in at: `oidc_issuer`, then `oidc_providers`. Empty with OIDC off."""
        if not (self.oidc_enabled and self.oidc_issuer and self.oidc_idp_id):
            return []
        try:
            primary = OidcProvider(
                issuer=self.oidc_issuer,
                idp_id=self.oidc_idp_id,
                discovery_url=self.oidc_discovery_url,
                subject_claims=None if self.oidc_subject_claims is None else tuple(self.oidc_subject_claims),
                connectors=self.oidc_connectors,
                client_marker=self.oidc_client_marker,
                allow_insecure=self.oidc_allow_insecure,
                lakehouse=self._primary_delegation(),
            )
        except ValidationError as exc:
            refusals = "; ".join(str(error["msg"]).removeprefix("Value error, ") for error in exc.errors())
            raise ValueError(f"WORKBENCH_OIDC_ISSUER's settings are refused: {refusals}") from None
        return [primary, *self.oidc_providers]

    def _primary_delegation(self) -> ProviderDelegation | None:
        """The primary issuer's delegation, from `LakehouseTokenSettings` where the service mixes it in: forward, or
        exchange with the workbench's client; ``None`` where it exchanges and names no client."""
        delegation: LakehouseDelegation | None = getattr(self, "lakehouse_delegation", None)
        if delegation is None:
            return None
        client_id, secret_file = getattr(self, "oidc_client_id", None), getattr(self, "oidc_client_secret_file", None)
        if delegation is LakehouseDelegation.EXCHANGE and not (client_id and secret_file):
            return None
        return ProviderDelegation(
            delegation=delegation, client_id=client_id, client_secret_file=secret_file, audience_scope=getattr(self, "lakehouse_audience_scope", None)
        )


class LakehouseTokenSettings:
    """How a person's read reaches the lakehouse, and the workbench's confidential client for its own token."""

    #: The lakehouse's audience: the `audience` of every exchange and of the workbench's own client-credentials token,
    #: and the audience a passed-on person's token must name.
    lakehouse_audience: str | None = Field(default=None, alias="WORKBENCH_LAKEHOUSE_AUDIENCE")
    #: The scope the workbench's own client asks for beside the `audience` parameter to be issued a token for the
    #: lakehouse, where the provider reads the audience from a scope (Dex's `audience:server:client_id:<audience>`).
    lakehouse_audience_scope: str | None = Field(default=None, alias="WORKBENCH_LAKEHOUSE_AUDIENCE_SCOPE")
    #: `exchange` (the default) or `forward` for the primary issuer's people (`WORKBENCH_OIDC_ISSUER`); a further issuer
    #: names its own (`OidcProvider.lakehouse`). See `LakehouseDelegation`.
    lakehouse_delegation: LakehouseDelegation = Field(default=LakehouseDelegation.EXCHANGE, alias="WORKBENCH_LAKEHOUSE_DELEGATION")
    oidc_client_id: str | None = Field(default=None, alias="WORKBENCH_OIDC_CLIENT_ID")
    #: The mounted secret file holding the client's secret, read on every request to the provider.
    oidc_client_secret_file: str | None = Field(default=None, alias="WORKBENCH_OIDC_CLIENT_SECRET_FILE")

    @model_validator(mode="after")
    def _validate_client(self) -> Self:
        # A service that only passes people's tokens on names the audience alone, and holds no client secret it never uses.
        client = [self.oidc_client_id, self.oidc_client_secret_file]
        if any(client) and not (all(client) and self.lakehouse_audience):
            raise ValueError("WORKBENCH_OIDC_CLIENT_ID and WORKBENCH_OIDC_CLIENT_SECRET_FILE are set together, and with WORKBENCH_LAKEHOUSE_AUDIENCE")
        return self


class GovernedAuthSettings(OidcSettings, FgaSettings):
    """Both halves, and their coupling: authorization asks about a person, so it needs one authenticated."""

    @model_validator(mode="after")
    def _authorization_requires_authentication(self) -> Self:
        if self.fga_enabled and not self.oidc_enabled:
            raise ValueError("WORKBENCH_OIDC_ENABLED is required when WORKBENCH_FGA_ENABLED is set (authorization needs a verified person)")
        return self
