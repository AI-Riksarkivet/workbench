"""The media trio is assembled ONCE .

`viewer`, `search` and `annotator` are three deployments of one shape — a Lance media service over
`workbench.media` — and each hand-assembled that shape in its own `main.py`. Three findings live on
that one surface:

* the lifespan body was copy-pasted three ways (build `AppState`, warm the default
  dataset handle off the loop, `attach_auth`, set the lifecycle flags, arm the SIGTERM drain, dispose
  in a `finally`) and the copies had already drifted: viewer disposed FGA before disarming the drain,
  search disarmed first, and the three closed different resource sets in different orders.
* **X12** — `create_viewer_app` / `create_search_app`, the declared test/composition seam, built a
  DIFFERENT app from production: no problem handlers on search's, no probes on either, no body cap on
  search's. A regression in exactly the layer their production comments describe (an
  `UnauthenticatedError` answering 500 instead of 401) could not be caught through the seam.
* `workbench` exported two different `register_middleware` functions under one name
  (`workbench.middleware` and `workbench.media.middleware`), imported bare by different callers,
  so a call site did not say which stack it registered.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from lance_namespace import UnauthenticatedError

from search.main import create_search_app
from workbench.media.config import MediaSettings
from workbench.media.state import AppState


def test_the_test_seam_app_maps_the_same_errors_production_does() -> None:
    """X12: an app built through the seam must translate the same exceptions the deployed one does.

    DRIVEN THROUGH A REQUEST, not by inspecting the handler table: starlette resolves a handler by
    MRO at dispatch, so "is `UnauthenticatedError` a key" answers the wrong question and would pass on
    an app that maps nothing. This raises the exact exception the OIDC verifier raises and reads the
    response.

    RED before the fix: `create_search_app` registered only `register_handlers`, which maps
    `DomainError` alone — so an expired or wrong-audience bearer came back `500 text/plain` from
    starlette's fallback, the very regression both mains' production comments are about.
    """
    app = create_search_app(AppState(settings=MediaSettings()))

    @app.get("/_probe_unauthenticated")
    async def _raise() -> None:
        raise UnauthenticatedError("expired token")

    response = TestClient(app, raise_server_exceptions=False).get("/_probe_unauthenticated")
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers
    assert response.json()["title"] == "UnauthenticatedError", response.json()
