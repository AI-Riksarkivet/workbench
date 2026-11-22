"""Viewer service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). Built by the kit's factory with the media services' composition onto it (``workbench.media.app``) and
their shared lifespan (``workbench.media.lifespan``): viewer, search and annotator are three deployments of one shape.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from viewer.api.v1.router import router as api_router
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.services.signed_media import ServiceReads
from workbench.media.app import build_media_app
from workbench.media.config import MediaSettings
from workbench.media.lifespan import make_media_lifespan, make_media_state
from workbench.media.state import AppState
from workbench.obs import configure_app_logging
from workbench.otel import TelemetrySinks


logger = logging.getLogger(__name__)

configure_app_logging(packages=("viewer", "workbench"))  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_viewer_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_viewer_settings()


def create_viewer_state(settings: MediaSettings) -> AppState:
    """The viewer's state: the media plane's (its registry and HTTP pool) and the viewer's own opens of the tables its
    signed media URLs name."""
    state = make_media_state(settings)
    state.service_reads = ServiceReads()
    return state


#: `closes` names the resources THIS service opens and must therefore close: the media plane's HTTP pool. The model
#: client slots of `AppState` are search's, which the viewer never fills.
lifespan = make_media_lifespan(
    _settings,
    service="viewer",
    make_state=create_viewer_state,
    closes=lambda state: (state.http,),
)


def build_app(settings: ViewerSettings | None = None, *, telemetry: TelemetrySinks | None = None) -> FastAPI:
    """The viewer as the pod serves it, with its lifespan; ``telemetry`` names other sinks than the OTLP exporters."""
    return build_media_app(title="viewer", settings=settings or get_viewer_settings(), routers=[api_router], lifespan=lifespan, telemetry=telemetry)


app = build_app()


def create_viewer_app(settings: MediaSettings | None = None, state: AppState | None = None) -> FastAPI:
    """Viewer app around shared or standalone state — the test/composition seam.

    THE SAME BUILDER production uses (X12), so an app built here maps every error the deployed one maps. No lifespan,
    because the caller supplies the state this app runs over.
    """
    resolved = settings or get_viewer_settings()
    test_app = build_media_app(title="viewer", settings=resolved, routers=[api_router])
    test_app.state.resources = state if state is not None else create_viewer_state(resolved)
    return test_app
