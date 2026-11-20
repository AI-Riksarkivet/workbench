"""Each media service's real app (the viewer, search, the annotator), run on settings a test names.

`running` builds the app the pod serves (`<service>.main.build_app`) on those settings, and points the two places that
read the service's settings outside the app at them too: its lifespan, which reads them when it starts
(`<service>.main._settings`), and its doors' settings dependency. ``telemetry`` names the sinks the service exports to,
the SDK's in-memory ones in a test (`tests.telemetry_sinks.MemorySinks`).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator import main as annotator_main
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from client_kit.media.config import MediaSettings
from search import main as search_main
from search.core.config import SearchSettings, get_search_settings
from service_kit.otel import TelemetrySinks
from viewer import main as viewer_main
from viewer.core.config import ViewerSettings, get_viewer_settings


#: The media services, by the name each exports its telemetry under.
SERVICES: Final = ("viewer", "search", "annotator")


class _Service:
    def __init__(self, settings: type[MediaSettings], getter: Callable[[], MediaSettings], module: object, build: Callable[..., FastAPI]) -> None:
        self.settings, self.getter, self.module, self.build = settings, getter, module, build


_SERVICES: Final = {
    "viewer": _Service(ViewerSettings, get_viewer_settings, viewer_main, viewer_main.build_app),
    "search": _Service(SearchSettings, get_search_settings, search_main, search_main.build_app),
    "annotator": _Service(AnnotatorSettings, get_annotator_settings, annotator_main, annotator_main.build_app),
}


@contextmanager
def running(service: str, env: Mapping[str, object], monkeypatch: pytest.MonkeyPatch, *, telemetry: TelemetrySinks | None = None) -> Generator[TestClient]:
    """``service``'s app on the settings ``env`` names, its lifespan entered."""
    chosen = _SERVICES[service]
    settings = chosen.settings.model_validate(dict(env))
    monkeypatch.setattr(chosen.module, f"get_{service}_settings", lambda: settings)
    app = chosen.build(settings, telemetry=telemetry)
    app.dependency_overrides[chosen.getter] = lambda: settings
    with TestClient(app) as client:
        yield client
