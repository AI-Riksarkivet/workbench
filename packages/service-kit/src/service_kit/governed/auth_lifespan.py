"""Put a service's sign-in and authorization in place on ``app.state``, once, from its lifespan.

``app.state.oidc`` verifies a person's token addressed to the workbench and ``app.state.dpop`` the proof that comes with
it; ``app.state.authz`` asks the workbench's own OpenFGA store; ``app.state.lakehouse_tokens`` exchanges a person's token
for one addressed to the lakehouse (under `LakehouseDelegation.EXCHANGE`) and takes the workbench's own. Each is ``None`` when its settings leave it off.

A failure to BUILD one is logged and leaves it ``None``: the door that needs it then answers 503, never a permissive
fallback, which would turn a broken authorization layer into an open one. Nothing is built at import. The store alone
is looked for again while the service runs (`_resolve_until_found`): an OpenFGA, a store or a model that arrives after
the boot deadline (a model writer that ran late, a store restored) is taken up without restarting the pod, and until
then its doors keep answering 503.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from starlette.concurrency import run_in_threadpool


if TYPE_CHECKING:
    from fastapi import FastAPI

    from service_kit.governed.fga import Authorizer
    from service_kit.governed.settings import OidcProvider
    from service_kit.governed.token_exchange import Delegation, LakehouseTokens, PersonTokens


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
    def oidc_idp_id(self) -> str | None: ...
    @property
    def oidc_person_providers(self) -> list[OidcProvider]: ...
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
    from service_kit.governed.token_exchange import PersonTokens

    app.state.oidc = None
    app.state.authz = None
    app.state.lakehouse_tokens = None
    app.state.delegations = {}
    app.state.person_tokens = PersonTokens()
    app.state.dpop = DpopVerifier(window_seconds=settings.dpop_window_seconds)
    if not settings.oidc_enabled:
        log.warning("%s: AUTHENTICATION IS OFF (WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED): every route serves anyone as anon", service)
    providers = settings.oidc_person_providers
    if settings.oidc_enabled and settings.oidc_audience and providers:
        try:
            from service_kit.governed.identity import PersonVerifier
            from service_kit.governed.oidc import OIDCVerifier

            app.state.oidc = PersonVerifier(
                OIDCVerifier(
                    [provider.issuer for provider in providers],
                    settings.oidc_audience,
                    settings.oidc_cache_ttl,
                    leeway=settings.oidc_leeway,
                    insecure_issuers=[provider.issuer for provider in providers if provider.allow_insecure],
                    discovery_overrides={provider.issuer: provider.discovery_url for provider in providers if provider.discovery_url} or None,
                ),
                providers,
            )
            # Proved at boot, off the loop, and never fatal: a wrong issuer is in this pod's log with the URL that
            # failed, rather than in a person's refused request minutes later.
            for issuer, reason in await run_in_threadpool(app.state.oidc.warm):
                log.error("%s: OIDC issuer unusable (issuer=%s): %s", service, issuer, reason)
        except Exception:
            log.exception("%s: OIDC verifier failed to build; every authenticated route will answer 503", service)
            app.state.oidc = None
        app.state.lakehouse_tokens, app.state.delegations = _lakehouse_clients(settings, providers, app.state.person_tokens)
        if app.state.lakehouse_tokens is None:
            log.warning("%s: no confidential client named; a lakehouse that authenticates refuses every read made as this service", service)
    app.state.authz_resolving = None
    if isinstance(settings, _FgaSettings) and settings.fga_enabled:
        app.state.authz = await _build_authorizer(settings, service)
        if app.state.authz is None:
            log.error("%s: no OpenFGA store %r holding this image's model; every authorized route answers 503 until it is found", service, settings.fga_store)
            app.state.authz_resolving = asyncio.create_task(_resolve_until_found(app, settings, service))


async def _build_authorizer(settings: _FgaSettings, service: str) -> Authorizer | None:
    """The store's authorizer, or ``None`` when it is not there by the deadline or its client cannot be built."""
    from service_kit.governed import fga

    try:
        return await fga.build_authorizer(
            settings.fga_api_url,
            credential_file=settings.fga_credential_file,
            store_name=settings.fga_store,
            model_id=settings.fga_model_id,
            deadline_seconds=fga.RESOLVE_DEADLINE_SECONDS,
            poll_seconds=fga.RESOLVE_POLL_SECONDS,
        )
    except Exception:
        log.exception("%s: OpenFGA client failed to build; every authorized route answers 503", service)
        return None


async def _resolve_until_found(app: FastAPI, settings: _FgaSettings, service: str) -> None:
    """Look for the store until it is there, then put its authorizer in place; cancelled by `dispose`."""
    from service_kit.governed import fga

    while True:
        await asyncio.sleep(fga.RESOLVE_POLL_SECONDS)
        authz = await _build_authorizer(settings, service)
        if authz is not None:
            app.state.authz = authz
            log.info("%s: OpenFGA store %r found; authorized routes answer again", service, settings.fga_store)
            return


def _lakehouse_clients(settings: object, providers: list[OidcProvider], people: PersonTokens) -> tuple[LakehouseTokens | None, dict[str, Delegation]]:
    """The workbench's own client at the primary issuer, for the service's own token, and each issuer's delegation: an
    exchange is made only at the issuer that signed the token, by the client named for that issuer."""
    from service_kit.governed.settings import LakehouseDelegation
    from service_kit.governed.token_exchange import Delegation, LakehouseTokens

    audience: str | None = getattr(settings, "lakehouse_audience", None)
    client_id: str | None = getattr(settings, "oidc_client_id", None)
    secret_file: str | None = getattr(settings, "oidc_client_secret_file", None)
    primary = providers[0]
    own = (
        LakehouseTokens(
            issuer=primary.issuer,
            audience=audience,
            client_id=client_id,
            client_secret_file=secret_file,
            people=people,
            audience_scope=getattr(settings, "lakehouse_audience_scope", None),
            discovery_url=primary.discovery_url,
        )
        if audience and client_id and secret_file
        else None
    )
    delegations: dict[str, Delegation] = {}
    for provider in providers:
        plan = provider.lakehouse
        if plan is None:
            continue
        tokens: LakehouseTokens | None = None
        if plan.delegation is LakehouseDelegation.EXCHANGE and audience and plan.client_id and plan.client_secret_file:
            tokens = (
                own
                if provider is primary and own is not None
                else LakehouseTokens(
                    issuer=provider.issuer,
                    audience=audience,
                    client_id=plan.client_id,
                    client_secret_file=plan.client_secret_file,
                    people=people,
                    audience_scope=plan.audience_scope,
                    discovery_url=provider.discovery_url,
                )
            )
        delegations[provider.issuer.rstrip("/")] = Delegation(mode=plan.delegation, tokens=tokens)
    return own, delegations


async def dispose(app: object) -> None:
    """Close what `attach_auth` opened. Suppressed: a shutdown that raises hides whatever came after it."""
    state = getattr(app, "state", None)
    # The verifier's refresher thread, which reads the issuers' documents in the background.
    verifier = getattr(state, "oidc", None)
    if verifier is not None:
        with suppress(Exception):
            await run_in_threadpool(verifier.close)
    resolving: asyncio.Task[None] | None = getattr(state, "authz_resolving", None)
    if resolving is not None:
        resolving.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await resolving
    authz = getattr(state, "authz", None)
    if authz is not None:
        await authz.close()
    clients = [getattr(state, "lakehouse_tokens", None)] + [plan.tokens for plan in getattr(state, "delegations", {}).values()]
    for tokens in {id(tokens): tokens for tokens in clients if tokens is not None}.values():
        with suppress(Exception):
            tokens.close()
