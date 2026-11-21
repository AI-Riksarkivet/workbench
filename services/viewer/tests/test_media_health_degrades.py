"""``/api/health`` answers 200 with no corpus dataset — it is a PROBE, not a dataset read — and tells anyone who asks
nothing of the service's internals.

A probe that answered 404 when no dataset resolves could not tell "service down" from "nothing loaded" (live-proof
2026-07-28, defect 4: a console 404 on every page load, and "backend unreachable" for a healthy service). Anyone may
call it, so it says only what is unavailable, never an exception, its message or an internal address.

Driven over HTTP at the boundary with the SAME problem+json handlers the service registers.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.media_catalog import lakehouse_dir
from viewer.api.v1.endpoints.system import router as system_router
from workbench.exceptions import register_handlers
from workbench.media.config import MediaSettings
from workbench.media.deps import get_state
from workbench.media.state import AppState


class _DeadEncoders:
    """The default encoder posture: nothing deployed, so every ping refuses, as httpx says it."""

    def get(self, url: str, timeout: float = 0.0) -> Any:
        raise httpx.ConnectError(f"All connection attempts failed to {url}")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """The default dataset names a namespace an empty directory catalog does not hold — an un-seeded corpus."""
    settings = MediaSettings.model_validate({**lakehouse_dir(str(tmp_path)), "WORKBENCH_DEFAULT_DATASET": "lake~acme$corpus"})
    state = AppState(settings=settings, http=_DeadEncoders())
    app = FastAPI()
    register_handlers(app)
    app.include_router(system_router)
    app.dependency_overrides[get_state] = lambda: state
    with TestClient(app) as c:
        yield c


def test_health_is_200_with_no_dataset_loaded_and_tells_no_internal_detail(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200, (
        f"/api/health returned {r.status_code} on a service with no corpus dataset: {r.text}. "
        "It is the media plane's liveness probe — the media + annotator zones poll it on every page "
        "load, so a non-200 here is a console error per load and a UI that blames the network."
    )
    # Anyone may call the probe: it says what is unavailable, never an exception, its message or an internal address.
    body = r.json()
    assert (body["db"], body["embed"]["ok"], body["rerank"]["ok"]) == (None, False, False)
    assert "Error" not in r.text
    assert "http" not in r.text
    assert "/media-corpus" not in r.text


def test_a_dataset_bound_endpoint_on_the_same_app_still_404s(client: TestClient) -> None:
    """The proof that the fix is scoped to the PROBE, not a blanket softening.

    Same app, same empty catalog: ``/api/columns`` reads a dataset, so its 404 is correct and stays.
    It is also the proof that this fixture has no dataset: the resolution path ``/api/health`` degrades
    on renders 404 problem+json here.
    """
    r = client.get("/api/columns")
    assert r.status_code == 404, f"a dataset READ with no dataset must still 404, got {r.status_code}"
    assert "lake~acme$corpus" in r.text
