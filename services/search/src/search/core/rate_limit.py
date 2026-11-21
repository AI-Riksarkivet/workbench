"""Search's per-route limiter: the instance, its rate, its key and its 429.

`/search`, `POST /search` and `/search/similar` all drive the same expensive resource, an embedding forward pass and a
cross-encoder rerank on top when it is asked for. They share one `scope`, so slowapi counts all three into one bucket
and they cannot be used in rotation for three times the budget. 30/minute is above interactive use and far below a
script's: a person refining a query does not reach it, a loop does at once.

PER ROUTE, NEVER A GLOBAL MIDDLEWARE: one number cannot be right for both `/search` and `/livez`, and a limited probe
fails the kubelet's checks under load, so pods cycle and the limiter becomes an outage. Probe routes carry no decorator.

THE LIMITER IS MODULE-LEVEL because `@limiter.limit(...)` is evaluated when the router is imported, so a limiter built
in the lifespan could not back it. Its storage is slowapi's in-memory backend, which is per process: the chart runs
search at `media.search.replicas: 1`, where that is the whole count. Each further replica adds its own bucket, so N
replicas allow N times the rate; shared storage is what scaling search out needs first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded


if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse


def by_subject(request: Request) -> str:
    """Key on the authenticated subject, falling back to the client IP.

    Keying by IP alone on an authenticated route puts everyone behind one address (a corporate NAT) in one bucket, and
    without the `ip:` fallback an anonymous caller would bypass the limit by presenting no credential.
    """
    subject = getattr(request.state, "subject", None)
    if subject:
        return f"user:{subject}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def make_limiter() -> Limiter:
    """A limiter keyed by :func:`by_subject`, on slowapi's in-memory backend (`storage_uri` unset)."""
    return Limiter(key_func=by_subject)


limiter = make_limiter()

#: One bucket for every search entry point; see the module docstring.
SEARCH_LIMIT = "30/minute"
SEARCH_SCOPE = "search"


def register_rate_limiting(app: FastAPI, limiter: Limiter) -> None:
    """Attach the limiter and a problem+json 429 that keeps `Retry-After`.

    `app.state.limiter` is the attribute slowapi's own header injection reads. A 429 without `Retry-After` gives a
    client nothing to pace itself by, so retries hammer the route back into 429; slowapi also sends `X-RateLimit-*` on
    an allowed response, so a client can slow down before it is refused.
    """
    from fastapi.responses import JSONResponse

    app.state.limiter = limiter

    async def _rate_limited(_request: Request, exc: Exception) -> JSONResponse:
        retry_after = "60"
        detail = "rate limit exceeded"
        if isinstance(exc, RateLimitExceeded):
            detail = f"rate limit exceeded: {exc.detail}"
            headers = getattr(exc, "headers", None) or {}
            retry_after = str(headers.get("Retry-After", retry_after))
        return JSONResponse(
            status_code=429,
            content={
                "type": "about:blank#ratelimitexceeded",
                "title": "Too Many Requests",
                "status": 429,
                "detail": detail,
            },
            headers={"Retry-After": retry_after},
            media_type="application/problem+json",
        )

    app.add_exception_handler(RateLimitExceeded, _rate_limited)
