"""CORS in every app the one factory builds: closed unless a deployment names an origin, and outermost when one is.

Starlette inserts each `add_middleware` at the front of the stack, so the LAST registration is the outermost. With CORS
outermost, a response another layer answers itself (the body-limit 413, the answer to an ordinary too-large upload)
carries the headers that let a named origin read it, rather than reaching the browser as an opaque network failure. The
app reaches every service same-origin through the Gateway, so no other origin is admitted unless the settings name one;
a preflight for the write methods the services route (the annotator's PUT/PATCH/DELETE) is answered with them.
"""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from workbench.app import make_service_app
from workbench.config import Settings


_ORIGIN = "https://ui.example"


def _app(**over: object) -> FastAPI:
    router = APIRouter()

    @router.api_route("/echo", methods=["POST", "PUT"])
    async def echo() -> dict[str, str]:
        return {"ok": "1"}

    settings = Settings.model_validate({"WORKBENCH_CORS_ORIGINS": [_ORIGIN], "WORKBENCH_API_PREFIX": "/api", **over})
    return make_service_app(title="cors-order", routers=[router], settings=settings)


def test_an_over_cap_rejection_is_readable_by_the_browser() -> None:
    with TestClient(_app(WORKBENCH_MAX_BODY_BYTES=16)) as client:
        response = client.post("/api/echo", content=b"x" * 64, headers={"Origin": _ORIGIN})
        assert response.status_code == 413
        assert response.headers.get("access-control-allow-origin") == _ORIGIN, f"the 413 is invisible to the caller that provoked it: {dict(response.headers)}"


def test_a_handled_response_keeps_its_cors_headers() -> None:
    with TestClient(_app()) as client:
        response = client.post("/api/echo", headers={"Origin": _ORIGIN})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == _ORIGIN
        assert "x-request-id" in response.headers.get("access-control-expose-headers", "").lower()


def test_a_named_origin_is_offered_the_write_methods() -> None:
    with TestClient(_app()) as client:
        response = client.options("/api/echo", headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "PUT"})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == _ORIGIN
        assert "PUT" in response.headers.get("access-control-allow-methods", "")


def test_no_other_origin_is_admitted_unless_one_is_named() -> None:
    with TestClient(_app(WORKBENCH_CORS_ORIGINS=[])) as client:
        preflight = client.options("/api/echo", headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "PUT"})
        assert "access-control-allow-origin" not in preflight.headers
