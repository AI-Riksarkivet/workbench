"""Put a service's sign-in and authorization in place on ``app.state``, once, from its lifespan.

``app.state.oidc`` verifies a person's token addressed to the workbench and ``app.state.dpop`` the proof that comes with
it; ``app.state.authz`` asks the workbench's own OpenFGA store; ``app.state.lakehouse_tokens`` exchanges a person's token
for one addressed to the lakehouse and takes the workbench's own. Each is ``None`` when its settings leave it off.

A failure to BUILD one is logged and leaves it ``None``: the door that needs it then answers 503, never a permissive
fallback, which would turn a broken authorization layer into an open one. Nothing is built at import.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from starlette.concurrency import run_in_threadpool


if TYPE_CHECKING:
    from fastapi import FastAPI


log = logging.getLogger(__name__)


class _AuthSettings(Protocol):
    """What `attach_auth` reads: `GovernedAuthSettings`, structurally, so a service's own class satisfies it."""

    @property
    def oidc_enabled(self) -> bool: ...
    @property
    def oidc_issuer(self) -> str | None: ...
    @property
    def oidc_audience(self) -> str | None: ...
    @property
    def oidc_discovery_url(self) -> str | None: ...
    @property
    def oidc_cache_ttl(self) -> int: ...
    @property
    def oidc_leeway(self) -> int: ...
    @property
    def oidc_allow_insecure(self) -> bool: ...
    @property
    def oidc_idp_id(self) -> str | None: ...
    @property
    def dpop_window_seconds(self) -> int: ...


@runtime_checkable
class _FgaSettings(Protocol):
    """`FgaSettings`, structurally: only a service that checks the workbench's own objects mixes it in."""

    @property
    def fga_enabled(self) -> bool: ...
    @property
    def fga_api_url(self) -> str: ...
    @property
    def fga_store(self) -> str: ...
    @property
    def fga_model_id(self) -> str | None: ...
    @property
    def fga_credential_file(self) -> str | None: ...


async def attach_auth(app: FastAPI, settings: _AuthSettings, *, service: str) -> None:
    """Build what ``settings`` turn on onto ``app.state``; see the module docstring. Call before the lifespan yields."""
    from service_kit.governed.dpop import DpopVerifier

    app.state.oidc = None
    app.state.authz = None
    app.state.lakehouse_tokens = None
    app.state.dpop = DpopVerifier(window_seconds=settings.dpop_window_seconds)
    if not settings.oidc_enabled:
        log.warning("%s: AUTHENTICATION IS OFF (WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED): every route serves anyone as anon", service)
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience and settings.oidc_idp_id:
        try:
            from service_kit.governed.identity import PersonVerifier
            from service_kit.governed.oidc import OIDCVerifier

            app.state.oidc = PersonVerifier(
                OIDCVerifier(
                    settings.oidc_issuer,
                    settings.oidc_audience,
                    settings.oidc_cache_ttl,
                    leeway=settings.oidc_leeway,
                    allow_insecure=settings.oidc_allow_insecure,
                    discovery_overrides={settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None,
                ),
                settings.oidc_idp_id,
            )
            # Proved at boot, off the loop, and never fatal: a wrong issuer is in this pod's log with the URL that
            # failed, rather than in a person's refused request minutes later.
            for issuer, reason in await run_in_threadpool(app.state.oidc.warm):
                log.error("%s: OIDC issuer unusable (issuer=%s): %s", service, issuer, reason)
        except Exception:
            log.exception("%s: OIDC verifier failed to build; every authenticated route will answer 503", service)
            app.state.oidc = None
        from service_kit.governed.token_exchange import LakehouseTokens

        app.state.lakehouse_tokens = LakehouseTokens.from_settings(settings)
        if app.state.lakehouse_tokens is None:
            log.warning("%s: no confidential client named; a lakehouse that authenticates refuses every read as a person", service)
    if isinstance(settings, _FgaSettings) and settings.fga_enabled:
        try:
            from service_kit.governed.fga import build_authorizer

            app.state.authz = await build_authorizer(
                settings.fga_api_url, credential_file=settings.fga_credential_file, store_name=settings.fga_store, model_id=settings.fga_model_id
            )
        except Exception:
            log.exception("%s: OpenFGA client failed to build; every authorized route will answer 503", service)
        if app.state.authz is None:
            log.error("%s: no OpenFGA store %r holding this image's model; every authorized route will answer 503", service, settings.fga_store)


async def dispose(app: object) -> None:
    """Close what `attach_auth` opened. Suppressed: a shutdown that raises hides whatever came after it."""
    state = getattr(app, "state", None)
    authz = getattr(state, "authz", None)
    if authz is not None:
        await authz.close()
    tokens = getattr(state, "lakehouse_tokens", None)
    if tokens is not None:
        with suppress(Exception):
            tokens.close()
