"""`send` refuses an item whose media dataset does not resolve, or belongs to a source not marked annotatable.

Removal is the escape hatch; this is what stops the trap being set. An item naming a dataset that
was renamed or removed cannot be opened on the canvas, so it can never be claimed, submitted or
skipped — and the publish precondition requires EVERY task terminal, so ONE of them wedges the whole
project. Creating it is the mistake, and send is where refusing it costs nothing: no task is written yet.

The interesting case is the one where refusing would be WRONG: a catalog that cannot be consulted (not being able to
check is not evidence of a problem). Driven on a real lance-namespace ``dir`` catalog and the real state store; the
unreachable catalog is a REST door on a port nothing listens on.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.project_events import router
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from annotator.projects.models import AnnotationProject, ProjectState, Task
from annotator.state.store import AnnotatorStore
from annotator.state.task_query import SortOrder, TaskFilter, TaskSort
from client_kit.media.config import Settings
from client_kit.media.deps import get_state
from client_kit.media.state import AppState
from tests.media_catalog import lakehouse_dir, lakehouse_rest, write_table


DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}


def _send(state_app: Callable[..., FastAPI], settings: Settings, body: dict[str, Any]) -> tuple[Any, list[Task]]:
    """Send ``body`` into a labeling project p1 and answer the response and the tasks the project then holds."""
    app = state_app(router)

    async def checker(**_kw: Any) -> bool:
        return True

    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    state = AppState(settings=settings)
    app.dependency_overrides[get_state] = lambda: state
    annotator = AnnotatorSettings.model_validate({"WORKBENCH_SOURCES": [source.model_dump(mode="json") for source in settings.sources.items]})
    app.dependency_overrides[get_annotator_settings] = lambda: annotator

    with TestClient(app) as client:
        store: AnnotatorStore = app.state.store

        async def seed() -> None:
            await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="vasa", state=ProjectState.LABELING))

        async def held() -> list[Task]:
            return (await store.task_page("p1", TaskFilter(), sort=TaskSort.ITEM, order=SortOrder.ASC, limit=200, offset=0))[2]

        portal = client.portal
        assert portal is not None, "the client runs no app"
        portal.call(seed)
        response = client.post("/projects/p1/items", json=body)
        return response, portal.call(held)


@pytest.fixture
def catalog(tmp_path: Path) -> Settings:
    """A directory catalog holding one dataset, ``lake:acme$demo``."""
    write_table(tmp_path, ["acme", "demo", "chunks"], pa.table({"doc_id": ["doc0"]}), descriptor=DESCRIPTOR)
    return Settings.model_validate({**lakehouse_dir(str(tmp_path))})


def _items(*wheres: str | None) -> dict[str, Any]:
    return {"items": [{"source": {"kind": "chunks", "keys": [f"doc{i}/0/1"], "where": w}, "media": {"kind": "image"}} for i, w in enumerate(wheres)]}


def test_an_item_naming_an_unknown_dataset_is_refused(state_app: Callable[..., FastAPI], catalog: Settings) -> None:
    r, held = _send(state_app, catalog, _items("lake~acme$gone-away"))

    assert r.status_code == 409, r.text
    assert "gone-away" in r.json()["detail"], "the refusal must name the dataset that does not exist"
    assert held == [], "a task was written for an item that can never be opened"


def _beside_the_lake(tmp_path: Path, *, annotatable: bool | None) -> list[dict[str, Any]]:
    """A lakehouse stand-in and an ``archive`` directory catalog holding ``archive~field$survey`` (its row table at
    versions 1 and 2), marked annotatable or not, or naming no mark."""
    write_table(tmp_path / "archive", ["field", "survey", "chunks"], pa.table({"doc_id": ["doc0"]}), descriptor=DESCRIPTOR)
    archive: dict[str, Any] = {"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(tmp_path / "archive")}}
    if annotatable is not None:
        archive["annotatable"] = annotatable
    return [{"kind": "rask", "name": "lake", "dir": str(tmp_path / "lake"), "identity": "none"}, archive]


@pytest.mark.parametrize("named", [True, False], ids=["named", "by-default"])
def test_an_item_of_a_source_not_marked_annotatable_is_refused_with_the_reason(state_app: Callable[..., FastAPI], tmp_path: Path, named: bool) -> None:
    """A source other than the lakehouse is annotated only where git marks it (`client_kit.sources.annotatable`): an item
    of an unmarked one, named or taken by default, is refused before the catalog is asked, naming why, and nothing is
    written; and the annotator refuses to start with such a default."""
    sources = _beside_the_lake(tmp_path, annotatable=None)
    settings = Settings.model_validate({"WORKBENCH_SOURCES": sources, "MEDIA_DEFAULT_DATASET": "archive~field$survey"})

    r, held = _send(state_app, settings, _items("archive~field$survey" if named else None))
    with pytest.raises(ValidationError, match="cannot be the annotator's default: source 'archive' is not marked annotatable"):
        AnnotatorSettings.model_validate({"WORKBENCH_SOURCES": sources, "MEDIA_DEFAULT_DATASET": "archive~field$survey"})

    assert (r.status_code, r.json()["detail"], held) == (409, "nothing was sent: archive~field$survey: source 'archive' is not marked annotatable", [])


def test_an_item_of_a_source_marked_annotatable_is_sent_with_its_table_and_version(state_app: Callable[..., FastAPI], tmp_path: Path) -> None:
    """A source git marks annotatable is sent from like the lakehouse: the item records the row table it was read from and
    the version the sender saw, held to that table as any version is."""
    settings = Settings.model_validate({"WORKBENCH_SOURCES": _beside_the_lake(tmp_path, annotatable=True)})
    body = _items("archive~field$survey")
    body["items"][0]["source"]["dataset_version"] = 1

    r, held = _send(state_app, settings, body)

    assert r.status_code == 201, r.text
    assert [(t.source.where, t.source.table, t.source.dataset_version) for t in held] == [("archive~field$survey", "archive~field$survey$chunks", 1)]


def test_a_known_dataset_sends_normally(state_app: Callable[..., FastAPI], catalog: Settings) -> None:
    r, held = _send(state_app, catalog, _items("lake~acme$demo"))

    assert r.status_code == 201, r.text
    assert [t.source.where for t in held] == ["lake~acme$demo"]


@pytest.mark.parametrize(
    ("version", "status", "kept"),
    [
        # The dataset's row table has versions 1 and 2 (its rows, then its descriptor stamp).
        pytest.param(1, 201, 1, id="an-honest-older-version-is-kept"),
        pytest.param(99, 409, None, id="a-fabricated-version-is-refused"),
        pytest.param(0, 422, None, id="version-zero-is-no-version"),
    ],
)
def test_an_items_version_is_kept_only_when_its_table_has_it(
    state_app: Callable[..., FastAPI], catalog: Settings, version: int, status: int, kept: int | None
) -> None:
    """The version an item names is the sender's, and the publish names it as an input: the send keeps it only once the
    catalog shows the stamped table at that version, and writes nothing otherwise."""
    body = _items("lake~acme$demo")
    body["items"][0]["source"]["dataset_version"] = version

    r, held = _send(state_app, catalog, body)

    assert r.status_code == status, r.text
    assert [(t.source.table, t.source.dataset_version) for t in held] == ([("lake~acme$demo$chunks", kept)] if kept else [])


def test_an_unverifiable_catalog_refuses_the_send_and_writes_nothing(state_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether the sender may read what they send is a check, and a check that did not run proves nothing: the send fails
    closed (503) and is tried again once the catalog answers."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    unreachable = Settings.model_validate({**lakehouse_rest(f"http://127.0.0.1:{closed}")})

    r, held = _send(state_app, unreachable, _items("lake~acme$anything"))

    assert r.status_code == 503, r.text
    assert held == []
