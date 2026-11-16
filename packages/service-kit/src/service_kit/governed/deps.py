"""The sign-in and authorization dependencies every workbench service's routes annotate with.

Who the request is: a token addressed to the workbench. Where the deployment's provider binds tokens to the browser's
key (`WORKBENCH_DPOP_REQUIRED`, on by default), it is presented with the DPoP scheme (``Authorization: DPoP <token>``)
and a proof in the ``DPoP`` header that the presenter holds the key the token is bound to (RFC 9449 § 7), and a bearer
is refused. Where the provider binds none, a ``Bearer`` token is accepted when it carries no ``cnf`` claim: a token that
names a key is accepted only with a proof that key signed, under the DPoP scheme, whatever the deployment says. Every
refusal is a 401 carrying the challenges the deployment accepts (§ 7.1, RFC 6750 § 3).

What they may do: the workbench's own OpenFGA store. With authorization off the checker allows everything; on but
unwired it answers 503, never a permissive fallback.

How they read the lakehouse: as the person, on a token the service exchanges for them (`LakehouseDelegation.EXCHANGE`),
or on the person's own token where the deployment passes it on (`LakehouseDelegation.FORWARD`): only a token that names
the lakehouse's audience and is bound to no key, unchanged and never widened.
"""

# NO `from __future__ import annotations`: the inner dependencies annotate a parameter with `settings_dep`, a local name
# FastAPI resolves at definition; a deferred annotation would silently demote it to a query parameter.
from collections.abc import Callable, Iterable
from typing import Annotated, Any, Final, Protocol

from fastapi import Depends, Request
from lance_namespace import UnauthenticatedError

from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.governed.dpop import ALGORITHMS, DpopVerifier, InvalidProofError
from service_kit.governed.fga import Authorizer, Grant
from service_kit.governed.identity import Caller, PersonVerifier
from service_kit.governed.oidc import ProviderUnavailableError
from service_kit.governed.settings import LakehouseDelegation
from service_kit.governed.token_exchange import Delegation, LakehouseCaller, LakehouseTokens, PersonTokens


#: The subject when authentication is off. Named so a grep for who can act without a token finds one place.
ANONYMOUS_SUBJECT: Final = "anon"

_ALGS: Final = " ".join(sorted(ALGORITHMS))


def _challenge(error: str | None, *, on: str, bearer: bool) -> dict[str, str]:
    """The `WWW-Authenticate` a 401 carries: the DPoP scheme and the algorithms a proof may use (RFC 9449 § 7.1), and,
    where a bearer is accepted too, the Bearer scheme first (RFC 6750 § 3); the error rides the scheme ``on`` names."""
    dpop = ([f'error="{error}"'] if error and on == "dpop" else []) + [f'algs="{_ALGS}"']
    challenges = [f"DPoP {', '.join(dpop)}"]
    if bearer:
        challenges.insert(0, f'Bearer error="{error}"' if error and on == "bearer" else "Bearer")
    return {"WWW-Authenticate": ", ".join(challenges)}


def _refuse(reason: str, detail: str, *, error: str | None, on: str, bearer: bool) -> UnauthorizedError:
    audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason=reason)
    return UnauthorizedError(detail, headers=_challenge(error, on=on, bearer=bearer))


class FgaChecker(Protocol):
    """The one question a route asks: may ``user`` hold ``relation`` on ``obj``, with ``contextual`` parent edges."""

    async def __call__(self, *, user: str, relation: str, obj: str, contextual: Iterable[Grant] = ()) -> bool: ...


async def _allow_all(*, user: str, relation: str, obj: str, contextual: Iterable[Grant] = ()) -> bool:
    """Authorization off: every check is allowed."""
    del user, relation, obj, contextual
    return True


ALLOW_ALL: Final[FgaChecker] = _allow_all


ANONYMOUS_CALLER: Final = LakehouseCaller(subject=ANONYMOUS_SUBJECT)


def _public_url(request: Request, settings: Any) -> str:
    """The URL a proof's `htu` must name: the configured public URL plus the path below the gateway's rewrite, or the
    request's own URL when none is configured.

    The path is the one the request carried on the wire (ASGI's ``raw_path``), percent-encoding and all, as the browser
    signed it: ``scope["path"]`` is percent-decoded, and decoding a reserved character changes the URL (RFC 3986 § 2.2),
    so a dataset id like ``acme%24letters`` would never match. The comparison normalises both sides (`dpop`)."""
    raw: bytes | None = request.scope.get("raw_path")
    path = raw.decode("ascii", "replace") if raw is not None else request.url.path
    public_url: str | None = getattr(settings, "public_url", None)
    if not public_url:
        return str(request.url.replace(path=path, query=""))
    mount: str = getattr(settings, "public_mount", "")
    if mount and (path == mount or path.startswith(f"{mount.rstrip('/')}/")):
        path = path[len(mount.rstrip("/")) :]
    return f"{public_url.rstrip('/')}{path}"


#: The principal key the lakehouse sees this service read as, on its own client-credentials token.
SERVICE_SUBJECT: Final = "workbench-service"


def _passed_on(settings: Any, caller: Caller, people: PersonTokens) -> LakehouseCaller:
    """The person's own token, passed on unchanged: only one that names the lakehouse's audience, so the lakehouse is
    handed nothing wider than the person's sign-in already asked for, and only one bound to no key (RFC 9449 § 7.1).

    The token the read presents is the person's LATEST such token, read at each catalog call (`PersonTokens`): a dataset
    handle cached for the person presents their renewed token, and none past its own `exp`.

    Raises:
        ServiceUnavailableError: the deployment names no lakehouse audience, or the person's token does not name it (the
            app's sign-in scopes do not ask for it), or it is bound: each is the deployment's to fix, not the person's.
        UnauthorizedError: the token has expired, so there is nothing of the person's to present.
    """
    audience: str | None = getattr(settings, "lakehouse_audience", None)
    if not audience:
        raise ServiceUnavailableError("WORKBENCH_LAKEHOUSE_DELEGATION=forward names no WORKBENCH_LAKEHOUSE_AUDIENCE to check the person's token against")
    if caller.bound:
        raise ServiceUnavailableError("the person's token is bound to their browser's key, so it is never passed on: exchange it instead")
    if not caller.addressed_to(audience):
        raise ServiceUnavailableError(f"the person's token does not name the lakehouse's audience {audience!r}: the app's sign-in scopes must ask for it")
    subject, issuer = caller.subject, caller.claims.iss
    people.remember(subject, issuer=issuer, token=caller.access_token.get_secret_value(), expires_at=caller.claims.exp)
    people.latest(subject, issuer=issuer)
    return LakehouseCaller(subject=subject, token=lambda: people.latest(subject, issuer=issuer).token)


def lakehouse_service_caller(request: Request, settings: Any) -> LakehouseCaller:
    """This service as the lakehouse reads it: its own client-credentials token (`LakehouseTokens.for_service`), or no
    token where no lakehouse catalog is read over its REST door. No person's token is held or used.

    Raises:
        ServiceUnavailableError: the service reads a lakehouse catalog and names no confidential client.
    """
    tokens: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
    if tokens is None:
        if getattr(settings, "reads_lakehouse", False):
            raise ServiceUnavailableError(
                "this service names no confidential client to read the lakehouse as itself: set WORKBENCH_OIDC_CLIENT_ID, "
                "WORKBENCH_OIDC_CLIENT_SECRET_FILE and WORKBENCH_LAKEHOUSE_AUDIENCE"
            )
        return LakehouseCaller(subject=SERVICE_SUBJECT)
    return LakehouseCaller(subject=SERVICE_SUBJECT, token=tokens.for_service, service=tokens.for_service)


class AuthDeps:
    """The dependencies one service's routes annotate with."""

    def __init__(
        self,
        authenticate: Callable[..., Caller | None],
        current_subject: Callable[..., str],
        get_checker: Callable[..., FgaChecker],
        get_authorizer: Callable[..., Authorizer | None],
        lakehouse_caller: Callable[..., LakehouseCaller],
        optional_lakehouse_caller: Callable[..., LakehouseCaller],
    ) -> None:
        self.authenticate = authenticate
        self.current_subject = current_subject
        self.get_checker = get_checker
        self.get_authorizer = get_authorizer
        self.lakehouse_caller = lakehouse_caller
        self.optional_lakehouse_caller = optional_lakehouse_caller


def make_auth_deps(settings_dep: Any) -> AuthDeps:
    """Build the dependencies against one service's settings type (`Annotated[TSettings, Depends(get_settings)]`).

    `Any` is the honest annotation for an `Annotated[...]` alias passed as a value.
    """

    def authenticate(request: Request, settings: settings_dep) -> Caller | None:
        """The person the request's token proves, with its DPoP proof where one is due; ``None`` with authentication off."""
        if not settings.oidc_enabled:
            return None
        verifier: PersonVerifier | None = getattr(request.app.state, "oidc", None)
        dpop: DpopVerifier | None = getattr(request.app.state, "dpop", None)
        if verifier is None or dpop is None:
            audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
            raise ServiceUnavailableError("Authentication is enabled but unavailable")
        required: bool = getattr(settings, "dpop_required", True)
        scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
        token = presented.strip()
        if not token:
            raise _refuse("missing_token", "Missing access token", error=None, on="dpop", bearer=not required)
        scheme = scheme.lower()
        if scheme not in ("dpop", "bearer") or (scheme == "bearer" and required):
            raise _refuse(
                "not_dpop",
                "The access token must be presented with the DPoP scheme",
                error="invalid_token",
                on="dpop" if required else "bearer",
                bearer=not required,
            )
        proofs = request.headers.getlist("dpop")
        if scheme == "dpop" and len(proofs) != 1:
            raise _refuse("missing_proof", "Exactly one DPoP proof is required", error="invalid_dpop_proof", on="dpop", bearer=not required)
        try:
            caller = verifier.verify(token)
        except ProviderUnavailableError:
            audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
            raise
        except UnauthenticatedError as exc:
            raise _refuse("invalid_token", "Invalid or expired token", error="invalid_token", on=scheme, bearer=not required) from exc
        if scheme == "bearer" and caller.bound:
            # RFC 9449 § 7.1: a token bound to a key is never accepted as a bearer, whatever the deployment says.
            raise _refuse("bound_as_bearer", "A DPoP-bound access token must be presented with its DPoP proof", error="invalid_token", on="bearer", bearer=True)
        if scheme == "dpop":
            try:
                dpop.verify(proofs[0], method=request.method, url=_public_url(request, settings), access_token=token, jkt=caller.jkt, person=caller.subject)
            except InvalidProofError as exc:
                raise _refuse("invalid_dpop_proof", str(exc), error="invalid_dpop_proof", on="dpop", bearer=not required) from exc
        audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject=caller.subject)
        return caller

    def current_subject(request: Request, caller: Annotated[Caller | None, Depends(authenticate)]) -> str:
        """The verified principal key, or ``anon`` with authentication off; published on `request.state` for a rate
        limiter, which keys a request by it."""
        subject = caller.subject if caller is not None else ANONYMOUS_SUBJECT
        request.state.subject = subject
        return subject

    def get_authorizer(request: Request, settings: settings_dep) -> Authorizer | None:
        """The workbench's store: ``None`` with authorization off, 503 when it is on and unwired."""
        if not getattr(settings, "fga_enabled", False):
            return None
        authz: Authorizer | None = getattr(request.app.state, "authz", None)
        if authz is None:
            audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, reason="client_unavailable")
            raise ServiceUnavailableError("Authorization is enabled but unavailable")
        return authz

    def get_checker(authz: Annotated[Authorizer | None, Depends(get_authorizer)]) -> FgaChecker:
        """The one-question checker over the store, or one that allows everything with authorization off."""
        return ALLOW_ALL if authz is None else authz.check

    def _as_lakehouse_caller(request: Request, settings: Any, caller: Caller | None) -> LakehouseCaller:
        person = _as_person(request, settings, caller)
        own: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
        # The workbench's own token rides beside the person's, for a lakehouse source the deployment reads as the
        # workbench (`client_kit.sources.SourceIdentity.SERVICE`); a source of any other kind is sent neither.
        return person if own is None else person.model_copy(update={"service": own.for_service})

    def _as_person(request: Request, settings: Any, caller: Caller | None) -> LakehouseCaller:
        if caller is None:
            return ANONYMOUS_CALLER
        issuer = caller.claims.iss.rstrip("/")
        delegations: dict[str, Delegation] = getattr(request.app.state, "delegations", {})
        plan = delegations.get(issuer)
        if plan is None:
            if getattr(settings, "reads_lakehouse_as_person", False):
                # A read with no token would be made anonymously on a person's behalf, and their token is never sent
                # to another issuer than its own: refused, never sent.
                raise ServiceUnavailableError(
                    f"the issuer {issuer} names no way to read the lakehouse as its people: for the primary issuer set "
                    "WORKBENCH_LAKEHOUSE_DELEGATION=forward, or WORKBENCH_OIDC_CLIENT_ID and WORKBENCH_OIDC_CLIENT_SECRET_FILE to "
                    "exchange; for a further issuer, its `lakehouse` entry in WORKBENCH_OIDC_PROVIDERS"
                )
            # No lakehouse is read over its REST door: no source this person reads takes a token.
            return LakehouseCaller(subject=caller.subject)
        people: PersonTokens = request.app.state.person_tokens
        if plan.mode is LakehouseDelegation.FORWARD:
            return _passed_on(settings, caller, people)
        tokens = plan.tokens
        if tokens is None:
            raise ServiceUnavailableError(f"the issuer {issuer}'s exchange names no client: set WORKBENCH_LAKEHOUSE_AUDIENCE and its client")
        subject = caller.subject
        tokens.remember(subject, caller.access_token.get_secret_value(), caller.claims.exp)
        return LakehouseCaller(subject=subject, token=lambda: tokens.for_person(subject))

    def lakehouse_caller(request: Request, settings: settings_dep, caller: Annotated[Caller | None, Depends(authenticate)]) -> LakehouseCaller:
        """The person a lakehouse read is made as, with their exchanged or passed-on token; anonymous with authentication
        off. With authentication on and no token to present, a read of a lakehouse catalog is a 503, never anonymous."""
        return _as_lakehouse_caller(request, settings, caller)

    def optional_lakehouse_caller(request: Request, settings: settings_dep) -> LakehouseCaller:
        """As `lakehouse_caller`, but anonymous when the request presents no token at all, for a probe that always
        answers; a token that is presented and fails is still refused, never read as anonymous."""
        if not request.headers.get("authorization"):
            return ANONYMOUS_CALLER
        return _as_lakehouse_caller(request, settings, authenticate(request, settings))

    return AuthDeps(
        authenticate=authenticate,
        current_subject=current_subject,
        get_checker=get_checker,
        get_authorizer=get_authorizer,
        lakehouse_caller=lakehouse_caller,
        optional_lakehouse_caller=optional_lakehouse_caller,
    )


__all__ = [
    "ALLOW_ALL",
    "ANONYMOUS_CALLER",
    "ANONYMOUS_SUBJECT",
    "SERVICE_SUBJECT",
    "AuthDeps",
    "FgaChecker",
    "LakehouseCaller",
    "lakehouse_service_caller",
    "make_auth_deps",
]
