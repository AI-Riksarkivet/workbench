"""Search's per-route limiter: the key it counts a caller by, the 429 it refuses with, and the probes it never limits.

`search.core.rate_limit` limits each search entry point per route, on slowapi's in-memory backend. These tests pin the
key (the authenticated subject, else the client address), the refusal (the shared problem+json envelope with a
`Retry-After` a client can pace itself by), and that a route carrying no limit, a probe above all, is never refused.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from search.core.rate_limit import by_subject, make_limiter, register_rate_limiting


def _request(state: dict[str, Any]) -> Request:
    """A real request from client 10.0.0.7 whose `request.state` holds `state`."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("10.0.0.7", 51000), "state": state})


def test_the_key_function_falls_back_to_the_client_address_for_an_anonymous_caller() -> None:
    """Without the `ip:` fallback an anonymous caller would escape the limit by presenting no credential."""
    assert by_subject(_request({})) == "ip:10.0.0.7"


def test_the_key_function_prefers_the_authenticated_subject() -> None:
    """Keyed by address on an authenticated route, everyone behind one NAT would share one bucket."""
    assert by_subject(_request({"subject": "alice@example.com"})) == "user:alice@example.com"


def test_a_429_is_the_shared_problem_envelope_with_retry_after() -> None:
    limiter = make_limiter()
    app = FastAPI()
    register_rate_limiting(app, limiter)

    @app.get("/expensive")
    @limiter.limit("1/minute")
    async def _expensive(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        assert client.get("/expensive").status_code == 200
        resp = client.get("/expensive")

    assert (resp.status_code, resp.headers["content-type"], resp.headers["retry-after"]) == (429, "application/problem+json", "60")
    assert resp.json() == {
        "type": "about:blank#rate-limited",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "rate limit exceeded: 1 per 1 minute",
    }


def test_a_route_with_no_limit_is_never_refused() -> None:
    """A limited probe would fail the kubelet's checks under load and cycle the pods; a probe carries no limit."""
    limiter = make_limiter()
    app = FastAPI()
    register_rate_limiting(app, limiter)

    @app.get("/livez")
    async def _livez() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        assert [client.get("/livez").status_code for _ in range(50)] == [200] * 50
