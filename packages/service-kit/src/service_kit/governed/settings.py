"""The workbench's sign-in and authorization settings, one ``WORKBENCH_*`` name each.

Three mixins, because the services have three needs:

* `OidcSettings`: verify a person's DPoP-bound token, addressed to the workbench's own audience, from any OIDC provider.
* `FgaSettings`: the workbench's own OpenFGA store, found by its name, and the model this image carries in it.
* `LakehouseTokenSettings`: the workbench's own confidential client at the provider, with which a service exchanges a
  person's token for one addressed to the lakehouse, and takes its own for the writes it makes as itself.

`GovernedAuthSettings` composes the first two with their coupling: authorization asks about a person, so it needs one
authenticated. Every credential is a mounted file (``*_FILE``), never a value in the environment.

Mix into a ``pydantic-settings`` ``BaseSettings`` subclass; they are plain mixins so the service's own class keeps its
``model_config``.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from service_kit.governed.dpop import DEFAULT_WINDOW_SECONDS
from service_kit.governed.fga import DEFAULT_STORE_NAME
from service_kit.governed.principal import MACHINE_IDPS, require_idp_id


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
    """Verify a person's token addressed to the workbench, and its DPoP proof."""

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
    #: The idp id the provider's subjects are principals of: a token's `sub` is the principal `<idp-id>~<sub>`.
    oidc_idp_id: str | None = Field(default=None, alias="WORKBENCH_OIDC_IDP_ID")
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
        if not self.oidc_allow_insecure and not self.oidc_issuer.startswith("https://"):
            raise ValueError("WORKBENCH_OIDC_ISSUER must use HTTPS (set WORKBENCH_OIDC_ALLOW_INSECURE=true for a dev provider)")
        if self.oidc_idp_id in MACHINE_IDPS:
            raise ValueError(f"WORKBENCH_OIDC_IDP_ID {self.oidc_idp_id!r} is a machine identity's idp id")
        require_idp_id(self.oidc_idp_id)
        return self


class LakehouseTokenSettings:
    """The workbench's confidential client at the provider, for tokens addressed to the lakehouse."""

    #: The lakehouse's audience: the `audience` of every exchange and of the workbench's own client-credentials token.
    lakehouse_audience: str | None = Field(default=None, alias="WORKBENCH_LAKEHOUSE_AUDIENCE")
    oidc_client_id: str | None = Field(default=None, alias="WORKBENCH_OIDC_CLIENT_ID")
    #: The mounted secret file holding the client's secret, read on every request to the provider.
    oidc_client_secret_file: str | None = Field(default=None, alias="WORKBENCH_OIDC_CLIENT_SECRET_FILE")

    @model_validator(mode="after")
    def _validate_client(self) -> Self:
        named = [self.lakehouse_audience, self.oidc_client_id, self.oidc_client_secret_file]
        if any(named) and not all(named):
            raise ValueError("WORKBENCH_LAKEHOUSE_AUDIENCE, WORKBENCH_OIDC_CLIENT_ID and WORKBENCH_OIDC_CLIENT_SECRET_FILE are set together")
        return self


class GovernedAuthSettings(OidcSettings, FgaSettings):
    """Both halves, and their coupling: authorization asks about a person, so it needs one authenticated."""

    @model_validator(mode="after")
    def _authorization_requires_authentication(self) -> Self:
        if self.fga_enabled and not self.oidc_enabled:
            raise ValueError("WORKBENCH_OIDC_ENABLED is required when WORKBENCH_FGA_ENABLED is set (authorization needs a verified person)")
        return self
