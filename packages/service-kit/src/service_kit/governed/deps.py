"""The sign-in and authorization dependencies every workbench service's routes annotate with.

Who the request is: a token addressed to the workbench, presented with the DPoP scheme (``Authorization: DPoP <token>``)
and a proof in the ``DPoP`` header that the presenter holds the key the token is bound to (RFC 9449 § 7). Every refusal
is a 401 carrying a ``WWW-Authenticate: DPoP`` challenge (§ 7.1). A bearer presented under the ``Bearer`` scheme is
refused: a DPoP-bound token must not be accepted without its proof.

What they may do: the workbench's own OpenFGA store. With authorization off the checker allows everything; on but
unwired it answers 503, never a permissive fallback.

How they read the lakehouse: as themselves, on a token the service exchanges for them (`LakehouseCaller`). Their own
token is never forwarded.
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
from service_kit.governed.token_exchange import LakehouseCaller, LakehouseTokens


#: The subject when authentication is off. Named so a grep for who can act without a token finds one place.
ANONYMOUS_SUBJECT: Final = "anon"

_ALGS: Final = " ".join(sorted(ALGORITHMS))


def _challenge(error: str | None = None) -> dict[str, str]:
    """The `WWW-Authenticate` a 401 carries: the DPoP scheme and the algorithms a proof may use (RFC 9449 § 7.1)."""
    params = ([f'error="{error}"'] if error else []) + [f'algs="{_ALGS}"']
    return {"WWW-Authenticate": f"DPoP {', '.join(params)}"}


def _refuse(reason: str, detail: str, *, error: str | None) -> UnauthorizedError:
    audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason=reason)
    return UnauthorizedError(detail, headers=_challenge(error))


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
    request's own URL when none is configured."""
    public_url: str | None = getattr(settings, "public_url", None)
    if not public_url:
        return str(request.url.replace(query=""))
    path = request.url.path
    mount: str = getattr(settings, "public_mount", "")
    if mount and (path == mount or path.startswith(f"{mount.rstrip('/')}/")):
        path = path[len(mount.rstrip("/")) :]
    return f"{public_url.rstrip('/')}{path}"


#: The principal key the lakehouse sees this service read as, on its own client-credentials token.
SERVICE_SUBJECT: Final = "workbench-service"


def lakehouse_service_caller(request: Request, settings: Any) -> LakehouseCaller:
    """This service as the lakehouse reads it: its own client-credentials token (`LakehouseTokens.for_service`), or no
    token on a local directory catalog, which authenticates nobody. No person's token is held or used.

    Raises:
        ServiceUnavailableError: the service reads a lakehouse catalog and names no confidential client.
    """
    tokens: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
    if tokens is None:
        if getattr(settings, "catalog_uri", None):
            raise ServiceUnavailableError(
                "this service names no confidential client to read the lakehouse as itself: set WORKBENCH_OIDC_CLIENT_ID, "
                "WORKBENCH_OIDC_CLIENT_SECRET_FILE and WORKBENCH_LAKEHOUSE_AUDIENCE"
            )
        return LakehouseCaller(subject=SERVICE_SUBJECT)
    return LakehouseCaller(subject=SERVICE_SUBJECT, token=tokens.for_service)


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
        """The person the request's DPoP-bound token and proof prove, or ``None`` with authentication off."""
        if not settings.oidc_enabled:
            return None
        verifier: PersonVerifier | None = getattr(request.app.state, "oidc", None)
        dpop: DpopVerifier | None = getattr(request.app.state, "dpop", None)
        if verifier is None or dpop is None:
            audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
            raise ServiceUnavailableError("Authentication is enabled but unavailable")
        scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
        if not token:
            raise _refuse("missing_token", "Missing DPoP-bound access token", error=None)
        if scheme.lower() != "dpop":
            raise _refuse("not_dpop", "The access token must be presented with the DPoP scheme", error="invalid_token")
        proofs = request.headers.getlist("dpop")
        if len(proofs) != 1:
            raise _refuse("missing_proof", "Exactly one DPoP proof is required", error="invalid_dpop_proof")
        try:
            caller = verifier.verify(token.strip())
        except ProviderUnavailableError:
            audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
            raise
        except UnauthenticatedError as exc:
            raise _refuse("invalid_token", "Invalid or expired token", error="invalid_token") from exc
        try:
            dpop.verify(proofs[0], method=request.method, url=_public_url(request, settings), access_token=token.strip(), jkt=caller.jkt, person=caller.subject)
        except InvalidProofError as exc:
            raise _refuse("invalid_dpop_proof", str(exc), error="invalid_dpop_proof") from exc
        tokens: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
        if tokens is not None:
            tokens.remember(caller.subject, caller.access_token.get_secret_value())
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
        if caller is None:
            return ANONYMOUS_CALLER
        tokens: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
        if tokens is None:
            if getattr(settings, "catalog_uri", None):
                # A read with no token would be made anonymously on a person's behalf: refused, never sent.
                raise ServiceUnavailableError(
                    "this service names no confidential client to read the lakehouse as the person: set WORKBENCH_OIDC_CLIENT_ID, "
                    "WORKBENCH_OIDC_CLIENT_SECRET_FILE and WORKBENCH_LAKEHOUSE_AUDIENCE"
                )
            # A local directory catalog authenticates nobody, so the read needs no token.
            return LakehouseCaller(subject=caller.subject)
        subject = caller.subject
        return LakehouseCaller(subject=subject, token=lambda: tokens.for_person(subject))

    def lakehouse_caller(request: Request, settings: settings_dep, caller: Annotated[Caller | None, Depends(authenticate)]) -> LakehouseCaller:
        """The person a lakehouse read is made as, with their exchanged token; anonymous with authentication off. With
        authentication on and no confidential client named, a read of a lakehouse catalog is a 503, never anonymous."""
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
