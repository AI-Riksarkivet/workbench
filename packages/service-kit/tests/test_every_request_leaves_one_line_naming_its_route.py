"""Every response a service sends leaves exactly one line naming its route's template, and nothing the request named.

uvicorn's access log is off for every service (the chart's and the images' commands): it prints the path and the query,
and a path can carry a credential (a signed media URL's token), a query a person's search. The factory writes the line
instead, whether telemetry is on or off: method, route template, status, whether the response completed, duration and
request id. It is the outermost layer that answers, so a refusal by a layer below the router (the body cap's 413, a
CORS preflight refused) is recorded too, and a handler that raises, before or after it started its response.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from service_kit import make_service_app
from service_kit.config import Settings


MARK = "marked-5f2e"
ALLOWED = "https://app.example"


@pytest.fixture
def client() -> Iterator[TestClient]:
    router = APIRouter()

    @router.api_route("/things/{name}", methods=["GET", "POST"])
    async def thing(name: str) -> dict[str, str]:
        return {"name": name}

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("the handler failed")

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            raise RuntimeError("the stream failed")

        return StreamingResponse(chunks())

    settings = Settings.model_validate({"WORKBENCH_MAX_BODY_BYTES": 16, "WORKBENCH_CORS_ORIGINS": [ALLOWED]})
    with TestClient(make_service_app(title="lines", routers=[router], settings=settings), raise_server_exceptions=False) as test_client:
        yield test_client


PREFLIGHT = {"Origin": "https://elsewhere.example", "Access-Control-Request-Method": "POST"}


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "line"),
    [
        pytest.param("GET", f"/api/v1/things/{MARK}?q={MARK}", {}, None, ("GET", "/api/v1/things/{name}", 200, True), id="routed"),
        pytest.param("GET", f"/api/v1/nowhere/{MARK}", {}, None, ("GET", "(no route)", 404, True), id="no-route"),
        pytest.param("POST", f"/api/v1/things/{MARK}", {}, b"x" * 64, ("POST", "(no route)", 413, True), id="over-the-cap"),
        pytest.param("OPTIONS", f"/api/v1/things/{MARK}", PREFLIGHT, None, ("OPTIONS", "(no route)", 400, True), id="refused-preflight"),
        pytest.param("GET", "/api/v1/boom", {}, None, ("GET", "/api/v1/boom", 500, False), id="handler-raised"),
        pytest.param("GET", "/api/v1/stream", {}, None, ("GET", "/api/v1/stream", 200, False), id="raised-mid-stream"),
    ],
)
def test_every_response_leaves_one_line_with_its_route_template_status_and_request_id(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    line: tuple[str, str, int, bool],
) -> None:
    caplog.set_level(logging.INFO, logger="service_kit.requests")

    client.request(method, path, headers={"X-Request-ID": "req-1", **headers}, content=body)

    lines = [record for record in caplog.records if record.name == "service_kit.requests"]
    fields = ("method", "route", "status", "complete", "request_id")
    assert [tuple(getattr(r, name, None) for name in fields) for r in lines] == [(*line, "req-1")]
    assert [key for r in lines for key, value in vars(r).items() if MARK in str(value)] == []


def test_a_probe_leaves_no_line(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="service_kit.requests")

    client.get("/readyz")
    client.get("/livez")

    assert [record for record in caplog.records if record.name == "service_kit.requests"] == []
