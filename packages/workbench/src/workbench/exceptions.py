"""The ONE domain exception hierarchy + RFC 9457 Problem Details handlers.

Routes raise these; never ``HTTPException`` directly. Each subclass maps to a status
code via the registered handler — one place to change the response shape.

This is the ONE copy: one ``_problem`` builder and its handlers, bound into ``make_service_app`` and shared by every
caller that raises a domain error, so two copies cannot drift.

``DomainError`` subclasses :class:`fastapi.HTTPException` because it is load-bearing: importing the exception class does NOT pull in the FastAPI
app/router/DI machinery, so non-web service/client modules import these freely, and a
service-layer raise stays catchable as an ``HTTPException`` while call sites read only the
domain class names.

Construct as ``ValidationError("stable detail msg")``; the message becomes ``exc.detail`` AND
``str(exc)`` (both the bare string — ``__str__`` is overridden so it is NOT Starlette's
``"400: msg"``). Keep the message STABLE; never interpolate a raw upstream exception into it
(log that instead). A refusal whose substance does not fit one string carries it as RFC 9457 §3.2
EXTENSION MEMBERS — ``raise Refused("2 problem(s)", extensions={"problems": [...]})`` — and a
subclass that wants to NAME its failure sets ``problem_type``. Both exist so a domain never has to
build a problem body beside this module.

The ``lance_namespace`` errors the auth plane raises, a validation error and an unexpected exception are answered by
:func:`workbench.ns_errors.install_problem_handlers`, which ``make_service_app`` installs after these, in the envelope of
``workbench.problem.problem_body``.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)

#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


class DomainError(HTTPException):
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"

    #: The RFC 9457 `type` URI. `None` means "derive it from the class name". A subclass sets it when the
    #: DOMAIN has a better name for the failure than its class name does — a graph that does not
    #: validate is `about:blank#flow-invalid`, not one derived from its exception class, and a
    #: client dispatching on `type` wants the specific one. Naming the failure is the taxonomy
    #: working, not a departure from it.
    problem_type: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        # Use the class-level status_code (subclasses override it) and the stable message;
        # default the detail to the title so an argument-less raise still yields a
        # meaningful, stable problem body.
        #
        # `headers` exists because `Retry-After` is part of what a 503 or a 429 MEANS — a refusal
        # that cannot say when to come back is a refusal the caller can only guess at.
        #
        # `extensions` are RFC 9457 §3.2 EXTENSION MEMBERS — additional keys on the problem body,
        # which the spec explicitly allows and tells consumers to ignore when unrecognised. They let a refusal whose
        # substance is STRUCTURED use this hierarchy rather than flatten it to one `detail` string. They can never shadow
        # a standard member (`_problem`).
        super().__init__(status_code=self.status_code, detail=detail or self.title, headers=headers)
        self.extensions: dict[str, Any] = dict(extensions or {})

    def __str__(self) -> str:
        # Starlette's default __str__ is "<status>: <detail>"; we want the bare stable
        # message so _problem()'s `str(exc)` == the detail.
        return self.detail


class ValidationError(DomainError):
    status_code = HTTPStatus.BAD_REQUEST
    title = "Bad Request"


class UnauthorizedError(DomainError):
    status_code = HTTPStatus.UNAUTHORIZED
    title = "Unauthorized"


class ForbiddenError(DomainError):
    status_code = HTTPStatus.FORBIDDEN
    title = "Forbidden"


class NotFoundError(DomainError):
    status_code = HTTPStatus.NOT_FOUND
    title = "Not Found"


class ConflictError(DomainError):
    status_code = HTTPStatus.CONFLICT
    title = "Conflict"


class ServiceUnavailableError(DomainError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Service Unavailable"


def _problem(exc: DomainError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": exc.problem_type or f"about:blank#{exc.__class__.__name__.lower()}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": str(exc) or exc.title,
    }
    # STANDARD MEMBERS FIRST, and extensions may not overwrite them. `type`, `title`, `status` and
    # `detail` are the four keys every client parses; an extension that could replace
    # one would let a refusal claim a status it does not have. RFC 9457 §3.2 permits the additional
    # members, not a redefinition of the ones it standardises.
    for key, value in exc.extensions.items():
        body.setdefault(key, value)
    return body


def problem_response(exc: DomainError) -> JSONResponse:
    """The problem+json answer of a domain error: its body, its status and the headers set where it was raised (a
    `Retry-After` is part of what a 503 or a 429 means, and a response built without it never carries one)."""
    return JSONResponse(status_code=exc.status_code, content=_problem(exc), media_type=PROBLEM_JSON, headers=exc.headers)


def route_template(request: Request) -> str | None:
    """The template of the route a request matched (``/api/signed-media/{token}``), or None where none matched: what a
    log line names a request by, never its path, which can carry a credential (a signed media URL's token)."""
    return scope_route_template(request.scope)


def scope_route_template(scope: Mapping[str, Any]) -> str | None:
    """:func:`route_template` of a routed ASGI scope. The route FastAPI (0.140) records for one of an included router's
    routes is that router's own, without the prefixes it is mounted under; the template it was matched by, prefixes and
    all, is on the effective route it records beside it."""
    effective = (scope.get("fastapi") or {}).get("effective_route_context")
    path = getattr(effective, "path", None) or getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


def register_handlers(app: FastAPI) -> None:
    """Map every :class:`DomainError` and ``RequestValidationError`` to problem+json.

    A `ServiceUnavailableError` raised from no other exception is a refusal the code chose (the issuer's key set not
    read again yet, no state store configured): its detail is the whole reason, so it is logged as one warning line
    with the detail, the request's method and its route's template (`route_template`) in ``extra=``, and no traceback.
    The detail rides in ``extra=``, which the formatter renders with ``repr`` (`context.DiagnosticFormatter`), because a
    detail can carry an upstream's own text, and a newline in the message would start a forged line. Measured
    2026-11-20: a traceback for each made 21 of them in two minutes of one service's log after the issuer restarted.
    Every other server-class error (>=500), a 503 raised from a cause included, is logged with the traceback, which is
    its diagnosis; client-class are not (they're expected), and a crash that is no domain error is `_unexpected`'s,
    traceback and all.
    """

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        if isinstance(exc, ServiceUnavailableError) and exc.__cause__ is None:
            log.warning(
                "service_unavailable",
                extra={"detail": exc.detail, "method": request.method, "route": route_template(request), "status": exc.status_code},
            )
        elif exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            log.exception("domain error", exc_info=exc, extra={"method": request.method, "route": route_template(request)})
        return problem_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            media_type=PROBLEM_JSON,
            content={
                "type": "about:blank#validation",
                "title": "Validation Error",
                "status": HTTPStatus.UNPROCESSABLE_ENTITY,
                "errors": [
                    {
                        "field": ".".join(str(p) for p in e["loc"]),
                        "message": e["msg"],
                        "type": e["type"],
                    }
                    for e in exc.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        """The CATCH-ALL, so anything that is not a `DomainError` is answered as problem+json too, never as
        starlette's `text/plain` 500, which no client parsing problem+json can read.

        A FIXED body, never the exception. The reference is explicit that internals reach logs only:
        the traceback goes to `log.exception` (inside the active OTel span), and the caller gets a
        stable title and detail. Leaking `str(exc)` here would put native/Arrow/S3 error text and
        filesystem paths on the wire for any unhandled fault.
        """
        log.exception("unhandled error", extra={"method": request.method, "route": route_template(request)})
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            media_type=PROBLEM_JSON,
            content={
                "type": "about:blank#internalerror",
                "title": "Internal Server Error",
                "status": HTTPStatus.INTERNAL_SERVER_ERROR,
                "detail": "Internal Server Error",
            },
        )
