"""`send` refuses an item whose media dataset does not resolve.

Removal is the escape hatch; this is what stops the trap being set. An item naming a dataset that
was renamed or removed cannot be opened on the canvas, so it can never be claimed, submitted or
skipped — and the publish precondition requires EVERY task terminal, so ONE of them wedges the whole
project. Creating it is the mistake, and send is where refusing it costs nothing: zero task actors
have been seeded yet.

The interesting case is the one where refusing would be WRONG: a catalog that cannot be consulted (not being able to
check is not evidence of a problem). Driven on a real lance-namespace ``dir`` catalog; the unreachable catalog is a REST
door on a port nothing listens on.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import project_events as events_ep
from annotator.api.v1.endpoints.project_events import router
from client_kit.media.config import Settings
from client_kit.media.deps import get_state
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import write_table


class _FakeProjectActor:
    async def get(self) -> dict[str, Any] | None:
        return {"project_id": "p1", "tenant": "acme", "slug": "vasa", "state": "labeling", "consensus_n": 1}

    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": payload["task_id"], "state": payload["state"], "counts": {}}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": payload["task_id"], "created": True}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"task_id": t["task_id"], "created": True} for t in payload["tasks"]], "counts": {}}


class _FakeTaskActor:
    def __init__(self) -> None:
        self.seeded: list[dict[str, Any]] = []

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seeded.append(payload)
        return payload


DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}


def _app(monkeypatch: pytest.MonkeyPatch, task_actor: _FakeTaskActor, settings: Settings) -> FastAPI:
    monkeypatch.setattr(events_ep, "_project_proxy", lambda _p: _FakeProjectActor())
    monkeypatch.setattr(events_ep, "_task_proxy", lambda _t: task_actor)

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    async def checker(**_kw: Any) -> bool:
        return True

    app.dependency_overrides[get_checker] = lambda: checker

    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    state = AppState(settings=settings)
    app.dependency_overrides[get_state] = lambda: state
    return app


@pytest.fixture
def catalog(tmp_path: Path) -> Settings:
    """A directory catalog holding one dataset, ``acme$demo``."""
    write_table(tmp_path, ["acme", "demo", "chunks"], pa.table({"doc_id": ["doc0"]}), descriptor=DESCRIPTOR)
    return Settings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)})


def _items(*wheres: str | None) -> dict[str, Any]:
    return {
        "items": [
            {
                "source": {"kind": "chunks", "keys": [f"doc{i}/0/1"], "where": w},
                "media": {"kind": "image"},
            }
            for i, w in enumerate(wheres)
        ]
    }


def test_an_item_naming_an_UNKNOWN_dataset_is_refused(monkeypatch: pytest.MonkeyPatch, catalog: Settings) -> None:
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor, catalog))

    r = client.post("/projects/p1/items", json=_items("acme$gone-away"))

    assert r.status_code == 409, r.text
    assert "gone-away" in r.text, "the refusal must name the dataset that does not exist"
    assert task_actor.seeded == [], "a task actor was seeded for an item that can never be opened"


def test_a_known_dataset_sends_normally(monkeypatch: pytest.MonkeyPatch, catalog: Settings) -> None:
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor, catalog))

    r = client.post("/projects/p1/items", json=_items("acme$demo"))

    assert r.status_code == 201, r.text
    assert len(task_actor.seeded) == 1


def test_an_UNVERIFIABLE_catalog_lets_the_send_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not being able to check is not evidence of a problem.

    If the catalog cannot be consulted at all, refusing every send would take the labeling
    plane down for a guard rather than a gate — the canvas still reports the real 404 at read, and
    removal still exists. This is the one place the check yields.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    unreachable = Settings.model_validate({"MEDIA_CATALOG_URI": f"http://127.0.0.1:{closed}"})
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor, unreachable))

    r = client.post("/projects/p1/items", json=_items("acme$anything"))

    assert r.status_code == 201, r.text
    assert len(task_actor.seeded) == 1
