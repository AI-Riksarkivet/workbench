"""A saved view shared by its link (ADR 0022): kept by the annotator, opened by anyone who may read its dataset, refused
as any read of that dataset is refused, and gone once its owner stops sharing it.

Driven over HTTP against the annotator's view doors on a real lance-namespace ``dir`` catalog and the real state store.
The source is the workbench's to decide, so a person's right to read it is the workbench's own check, as on every other
read of it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import _deps, current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.views import router
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from tests.media_catalog import write_table
from workbench.governed.token_exchange import LakehouseCaller
from workbench.media.config import MediaSettings
from workbench.media.deps import get_state
from workbench.media.state import AppState


DATASET = "lake~acme$demo"
#: What the app keeps for a view: its arrangement, the table in view, its set-ups and its search. Opaque here.
SNAPSHOT = {"layout": {"root": {"type": "row", "content": []}}, "table": "chunks", "setups": {"atlas": {"color": "lang"}}, "q": "vasa", "node": None}


def test_a_shared_view_is_shared_and_opened_only_as_a_read_listed_to_its_owner_alone_and_closes_when_they_stop_sharing(
    state_app: Callable[..., FastAPI], tmp_path: Path
) -> None:
    write_table(tmp_path, ["acme", "demo", "chunks"], pa.table({"doc_id": ["doc0"]}), descriptor={"identity": {"key_fields": ["doc_id"]}})
    settings = MediaSettings.model_validate(
        {"WORKBENCH_SOURCES": [{"kind": "namespace", "name": "lake", "impl": "dir", "properties": {"root": str(tmp_path)}}]}
    )
    app = state_app(router)
    who = {"subject": "gina"}

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return not (user == "hal" and relation == "can_read" and obj == "source:lake")

    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: who["subject"]
    app.dependency_overrides[_deps.lakehouse_caller] = lambda: LakehouseCaller(subject=who["subject"])
    state = AppState(settings=settings)
    app.dependency_overrides[get_state] = lambda: state
    annotator = AnnotatorSettings.model_validate({"WORKBENCH_SOURCES": [source.model_dump(mode="json") for source in settings.sources.items]})
    app.dependency_overrides[get_annotator_settings] = lambda: annotator

    def as_(subject: str, method: str, path: str, **kw: Any) -> Any:
        who["subject"] = subject
        return client.request(method, path, **kw)

    with TestClient(app) as client:
        shared = as_("gina", "POST", "/views", json={"dataset": DATASET, "name": "Vasa in Swedish", "snapshot": SNAPSHOT})
        assert shared.status_code == 201, shared.text
        view_id = shared.json()["view_id"]

        opened = as_("ida", "GET", f"/views/{view_id}")
        refused = as_("hal", "GET", f"/views/{view_id}")
        planted = as_("hal", "POST", "/views", json={"dataset": DATASET, "name": "Planted", "snapshot": SNAPSHOT})
        listed = as_("gina", "GET", "/views", params={"dataset": DATASET})
        listed_to_ida = as_("ida", "GET", "/views", params={"dataset": DATASET})
        not_hers = as_("ida", "DELETE", f"/views/{view_id}")
        stopped = as_("gina", "DELETE", f"/views/{view_id}")
        after = as_("ida", "GET", f"/views/{view_id}")

    assert opened.status_code == 200, opened.text
    assert {k: opened.json()[k] for k in ("view_id", "dataset", "name", "owner", "snapshot")} == {
        "view_id": view_id,
        "dataset": DATASET,
        "name": "Vasa in Swedish",
        "owner": "gina",
        "snapshot": SNAPSHOT,
    }
    assert (refused.status_code, refused.json().get("detail")) == (403, "hal may not read source 'lake'")
    assert (planted.status_code, planted.json().get("detail")) == (403, "hal may not read source 'lake'")
    assert ([view["view_id"] for view in listed.json()["views"]], listed.json()["more"]) == ([view_id], False)
    assert listed_to_ida.json() == {"views": [], "more": False}
    assert not_hers.status_code == 404
    assert stopped.status_code == 204
    assert after.status_code == 404
