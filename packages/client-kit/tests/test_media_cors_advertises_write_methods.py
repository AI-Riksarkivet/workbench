"""The media CORS layer: closed unless a deployment names an origin, outermost, and advertising the write methods its
apps serve.

The app reaches every service same-origin through the Gateway, so a media app admits no other origin unless its settings
name one. When one is named, CORS is the outermost layer, so a response another layer answers itself (the body cap's
413, the request id's) carries the headers that let the browser read it, and a preflight for the annotator's
PUT/PATCH/DELETE routes is answered with them. Guards the shared seam of the viewer, search and the annotator at once.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from client_kit.media.config import Settings as MediaSettings
from client_kit.media.middleware import register_media_middleware
from tests.media_catalog import lakehouse_dir


_ORIGIN = "https://example.test"


def _app(**settings: object) -> TestClient:
    app = FastAPI()
    register_media_middleware(app, MediaSettings.model_validate({**lakehouse_dir("/catalog"), "MEDIA_MAX_BODY_BYTES": 16, **settings}))

    @app.api_route("/thing", methods=["POST", "PUT"])
    async def _thing(request: Request) -> dict[str, int]:
        return {"read": len(await request.body())}

    return TestClient(app)


def test_no_other_origin_is_admitted_unless_one_is_named() -> None:
    preflight = _app().options("/thing", headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "PUT"})

    assert "access-control-allow-origin" not in preflight.headers


def test_a_named_origin_is_answered_on_every_response_and_offered_the_write_methods() -> None:
    client = _app(MEDIA_CORS_ORIGINS=[_ORIGIN])

    preflight = client.options("/thing", headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "PUT"})
    over_cap = client.post("/thing", content=b"x" * 64, headers={"Origin": _ORIGIN})

    assert "PUT" in preflight.headers.get("access-control-allow-methods", "")
    assert over_cap.status_code == 413
    assert over_cap.headers.get("access-control-allow-origin") == _ORIGIN, f"the 413 is unreadable by the caller that provoked it: {dict(over_cap.headers)}"
