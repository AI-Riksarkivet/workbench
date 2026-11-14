"""Every annotator route a browser can be routed to verifies the person: a DPoP-bound token and its proof.

The fixture's HTTPRoute publishes the annotator's doors one prefix at a time (`deploy/fixtures/annotator`'s
`route.doors`), so whichever prefix is published next, a route under it must already resolve the door that verifies the
caller (`service_kit.governed.deps`' `authenticate`). Deny by default: the walk requires it of every route of the app
the pod serves, except the routes in the table below, each with the reason it takes no person's token. A route added
later arrives gated or argued, never silently open.

What a verified person may then do is each handler's own role check on the workbench's store, pinned by the doors' own
tests; this walk pins authentication only.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute


#: Routes that take no person's token, each with why. None of them is under a published door.
EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/livez"): "process liveness, dependency-free by contract",
    ("GET", "/readyz"): "readiness, no person's data",
    ("GET", "/healthz"): "the sidecar's health probe, no person's data",
    ("GET", "/dapr/config"): "the sidecar reads the actor configuration from its own app",
    ("DELETE", "/actors/{actor_type_name}/{actor_id}"): "the sidecar's actor callback, inside the pod",
    ("PUT", "/actors/{actor_type_name}/{actor_id}/method/{method_name}"): "the sidecar's actor callback, inside the pod",
    ("PUT", "/actors/{actor_type_name}/{actor_id}/method/timer/{timer_name}"): "the sidecar's actor callback, inside the pod",
    ("PUT", "/actors/{actor_type_name}/{actor_id}/method/remind/{reminder_name}"): "the sidecar's actor callback, inside the pod",
    ("POST", "/api/jobs/apply"): "a batch job's door, called service to service with the sidecar's app token, never published",
    ("GET", "/api/jobs/{job_id}"): "a batch job's status, called service to service with the sidecar's app token, never published",
}


def _app() -> FastAPI:
    """The module-level app: the composition the pod serves."""
    from annotator.main import app

    return app


def _routes(app: FastAPI) -> tuple[list[APIRoute], list[str]]:
    """Every APIRoute, descending the included routers (this FastAPI keeps them as wrappers holding `original_router`),
    and every other route the app serves. Anything that is neither (a Starlette `Route`, `Mount` or `WebSocketRoute`)
    takes no FastAPI dependency at all, so the walk cannot see whether it verifies the person: it is refused."""
    found: list[APIRoute] = []
    other: list[str] = []
    stack = list(app.routes)
    while stack:
        item = stack.pop()
        if isinstance(item, APIRoute):
            found.append(item)
            continue
        inner = getattr(item, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        other.append(f"{type(item).__name__} {getattr(item, 'path', '?')}")
    return found, other


def _verifies_the_person(route: APIRoute) -> bool:
    from annotator.api import security

    door = security.authenticate

    def _walk(dependant: Dependant) -> bool:
        return dependant.call is door or any(_walk(sub) for sub in dependant.dependencies)

    return _walk(route.dependant)


def test_every_route_but_the_exempt_ones_verifies_the_person() -> None:
    api, other = _routes(_app())
    routes = [(method, route) for route in api for method in route.methods or ()]
    open_routes = sorted((method, route.path) for method, route in routes if (method, route.path) not in EXEMPT and not _verifies_the_person(route))
    walked = {(method, route.path) for method, route in routes}

    assert (open_routes, sorted(other)) == ([], [])
    assert set(EXEMPT) <= walked, f"exempt routes the app no longer serves: {sorted(set(EXEMPT) - walked)}"
