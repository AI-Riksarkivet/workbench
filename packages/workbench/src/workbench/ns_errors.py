"""The problem+json answer for every error a service does not answer itself.

A lance-namespace call a door lets through raises that SDK's error hierarchy (``LanceNamespaceError`` with a numeric
``.code``); this module maps those codes to HTTP statuses and RFC 9457 problem details, and gives a validation error, a
framework 404 or 405 and an unexpected exception the same envelope.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from lance_namespace import ErrorCode, LanceNamespaceError

from workbench.outage import reads_as_store_outage
from workbench.problem import PROBLEM_JSON, occurrence, problem_body


if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["install_problem_handlers", "problem_detail", "status_for"]

_STATUS: dict[ErrorCode, int] = {
    # 406, as the spec's `UnsupportedOperationErrorResponse` is on every operation that lists it: a 501 is retried by
    # nothing and alerted on by most things, while 406 says the request asked for something this backend does not do.
    ErrorCode.UNSUPPORTED: 406,
    ErrorCode.NAMESPACE_NOT_FOUND: 404,
    ErrorCode.NAMESPACE_ALREADY_EXISTS: 409,
    ErrorCode.NAMESPACE_NOT_EMPTY: 409,
    ErrorCode.TABLE_NOT_FOUND: 404,
    ErrorCode.TABLE_ALREADY_EXISTS: 409,
    ErrorCode.TABLE_INDEX_NOT_FOUND: 404,
    ErrorCode.TABLE_INDEX_ALREADY_EXISTS: 409,
    ErrorCode.TABLE_TAG_NOT_FOUND: 404,
    ErrorCode.TABLE_TAG_ALREADY_EXISTS: 409,
    ErrorCode.TRANSACTION_NOT_FOUND: 404,
    ErrorCode.TABLE_VERSION_NOT_FOUND: 404,
    ErrorCode.TABLE_COLUMN_NOT_FOUND: 404,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.CONCURRENT_MODIFICATION: 409,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
    ErrorCode.INVALID_TABLE_STATE: 409,
    ErrorCode.TABLE_SCHEMA_VALIDATION_ERROR: 400,
    ErrorCode.THROTTLING: 429,
    ErrorCode.TABLE_BRANCH_NOT_FOUND: 404,
    ErrorCode.TABLE_BRANCH_ALREADY_EXISTS: 409,
}


def status_for(code: int) -> int:
    """Map a numeric lance ``ErrorCode`` to an HTTP status, defaulting to 500."""
    try:
        return _STATUS.get(ErrorCode(code), 500)
    except ValueError:
        return 500


def problem_detail(exc: LanceNamespaceError) -> tuple[int, dict[str, object]]:
    """Build (status, RFC 9457 problem+json body) for a domain error.

    A 5xx-mapped error uses a GENERIC ``detail``, never ``str(exc)``, so internals (paths, DSNs, driver messages) reach
    the logs only, never the response body. A client (4xx) error keeps its message: it is actionable and names only
    what the caller sent.
    """
    status = status_for(int(exc.code))
    detail = str(exc) if status < 500 else "Internal Server Error"
    body: dict[str, object] = {
        "type": f"https://lance.org/problems/{exc.__class__.__name__.lower()}",
        "title": exc.__class__.__name__,
        "status": status,
        "detail": detail,
        "code": int(exc.code),
        # Spec-0.9 ErrorResponse compatibility: `code` (required) + `error` (brief message). Kept
        # alongside the RFC 9457 fields so both problem-details and spec clients can parse us.
        "error": detail,
        **occurrence(),
    }
    return status, body


#: The spec code to stamp on a status the framework produced, where the spec HAS one for it.
#:
#: The inverse of the `ErrorCode -> status` map above, and deliberately partial: a status with no
#: domain meaning (404 on an unknown route, 405, 409 from app code) falls through to `Unsupported`,
#: which is the truthful answer for "this backend does not do that". Guessing `TableNotFound` for a
#: routing 404 would tell a client a table is missing when the OPERATION is.
_STATUS_CODE_FALLBACK: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_INPUT,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    429: ErrorCode.THROTTLING,
    500: ErrorCode.INTERNAL,
    501: ErrorCode.UNSUPPORTED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def install_problem_handlers(app: FastAPI, log: logging.Logger) -> None:
    """Register the problem+json exception handlers every service app shares.

    One home keeps the RFC 9457 shapes, the 5xx-generic-detail policy and the leak rules from drifting per service.
    A log line names a request by its route's template (`route_template`), never its path, which can carry a credential.
    """
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from workbench.exceptions import route_template

    @app.exception_handler(LanceNamespaceError)
    async def handle_domain_error(request: Request, exc: LanceNamespaceError) -> JSONResponse:
        # The native backend reports an object store that did not answer as a typed InternalError (500);
        # it is the same outage `handle_unexpected_error` answers 503 for, in the backend's own dialect.
        if int(exc.code) == ErrorCode.INTERNAL and reads_as_store_outage(exc):
            return _store_unavailable(request, exc)
        status, body = problem_detail(exc)
        # A capability answer, not a fault: an info line, never a traceback, so an unsupported operation does not read
        # as an outage.
        if status == status_for(ErrorCode.UNSUPPORTED):
            log.info("unsupported_operation", extra={"method": request.method, "route": route_template(request), "status": status})
        elif status >= 500:
            log.exception("domain_error", extra={"method": request.method, "route": route_template(request), "status": status})
        return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)

    def _store_unavailable(request: Request, exc: Exception) -> JSONResponse:
        log.warning("object_store_unavailable", extra={"method": request.method, "route": route_template(request), "error": str(exc)[:300]})
        return JSONResponse(
            status_code=503,
            content=problem_body(ErrorCode.SERVICE_UNAVAILABLE, status=503, title="ServiceUnavailable", detail="Service Unavailable"),
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        status = 422
        return JSONResponse(
            status_code=status,
            # The shared envelope plus the field list.
            content=problem_body(ErrorCode.INVALID_INPUT, status=status, title="Validation Error", detail="Validation Error", slug="validation")
            | {"errors": [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]},
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_framework_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """FastAPI's own 404 and 405 (and any explicit `HTTPException`), in the same envelope, with a `code`.

        `Unsupported` is the default: neither "no such route" nor "wrong method" is a domain condition. A status that
        has a code keeps it, so an explicit `HTTPException(401)` still reads as `Unauthenticated`, and the exception's
        own `detail` survives. Registered after the domain handler, so a `LanceNamespaceError` keeps its own code.
        """
        code = _STATUS_CODE_FALLBACK.get(exc.status_code, ErrorCode.UNSUPPORTED)
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        phrase = HTTPStatus(exc.status_code).phrase
        # An explicit slug, because the default derives the `type` URI from the title and the phrases
        # carry spaces — `.../problems/method not allowed` is not a URI a client can key on.
        body = problem_body(code, status=exc.status_code, title=phrase, detail=detail, slug=phrase.lower().replace(" ", "-"))
        return JSONResponse(status_code=exc.status_code, content=body, headers=dict(exc.headers or {}), media_type=PROBLEM_JSON)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # AN OBJECT STORE THAT DID NOT ANSWER IS AN OUTAGE, and every Lance call reaches this handler
        # through the same bare `ValueError`/`OSError`. Classified here, at the one seam every untyped
        # failure passes, rather than at each of the call sites that open, scan or commit: a 503 tells
        # the caller to retry and the operator to look at the store, where a 500 says this service broke.
        if reads_as_store_outage(exc):
            return _store_unavailable(request, exc)
        # Internals (native/Arrow/S3 error text, paths) leak via logs only — never the body.
        log.exception("unhandled_error", extra={"method": request.method, "route": route_template(request)})
        return JSONResponse(
            status_code=500,
            content=problem_body(ErrorCode.INTERNAL, status=500, title="InternalError", detail="Internal Server Error"),
            media_type=PROBLEM_JSON,
        )
