"""Search service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). Built by the kit's factory with the media services' composition onto it (``client_kit.media.app``) and
their shared lifespan (``client_kit.media.lifespan``): viewer, search and annotator are three deployments of one shape.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from client_kit.media.app import build_media_app
from client_kit.media.config import MediaSettings
from client_kit.media.lifespan import make_media_lifespan
from client_kit.media.state import AppState
from client_kit.rate_limit import register_rate_limiting
from search.api.v1.router import router as api_router
from search.core.config import SearchSettings, get_search_settings
from search.core.rate_limit import limiter
from service_kit import setup_logging
from service_kit.obs import configure_app_logging
from service_kit.otel import TelemetrySinks


logger = logging.getLogger(__name__)

configure_app_logging(packages=("search", "client_kit"))  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)

# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()


def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_search_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_search_settings()


#: `attach_auth` inside the shared lifespan IS THE LINE THAT MAKES THE DOOR FUNCTION. `api/security.py` builds the deps
#: and every route declares them, but they find the verifier on `app.state`, and enabled with none there is a 503, never
#: a permissive fallback; hence one lifespan for every media service, and
#: `services/search/tests/test_the_search_door_is_wired.py`, which runs this lifespan and requires a challenge rather
#: than a 503.
lifespan = make_media_lifespan(
    _settings,
    service="search",
    closes=lambda state: (state.http, state.embedder, state.reranker),
)


def _with_rate_limiting(app: FastAPI) -> FastAPI:
    """PER-ROUTE rate limiting for the GPU surfaces (see search.core.rate_limit).

    Applied here rather than inside `build_media_app` because it is the search plane's alone: slowapi
    reads `app.state.limiter` by that exact name for its header injection, and the problem+json 429
    replaces slowapi's default so a refusal is shaped like every other error in the estate. It runs
    AFTER the shared handlers for the same reason it always did — its 429 handler is the last word on
    that status.
    """
    register_rate_limiting(app, limiter)
    return app


def build_app(settings: SearchSettings | None = None, *, telemetry: TelemetrySinks | None = None) -> FastAPI:
    """Search as the pod serves it, with its lifespan; ``telemetry`` names other sinks than the OTLP exporters."""
    return _with_rate_limiting(
        build_media_app(title="search", settings=settings or get_search_settings(), routers=[api_router], lifespan=lifespan, telemetry=telemetry)
    )


app = build_app()


def create_search_app(state: AppState, settings: SearchSettings | None = None) -> FastAPI:
    """Search app around externally-opened state — the test/composition seam.

    THE SAME BUILDER production uses (X12), so an app built here maps every error, and shapes every 429, as the deployed
    one does. No lifespan, because the caller supplies the state this app runs over.
    """
    test_app = _with_rate_limiting(build_media_app(title="search", settings=settings or get_search_settings(), routers=[api_router]))
    test_app.state.resources = state
    return test_app
