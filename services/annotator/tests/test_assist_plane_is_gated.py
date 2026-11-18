"""The interactive AI-assist routes must have a door, like every router beside them.

open_fastapi-audit — "The annotator's interactive AI-assist plane is three routes with no subject,
no checker and no catalog delegation — it spends model compute for any caller who can reach the pod".

`assist.py` declared `router = APIRouter(prefix="/api", tags=["assist"])` with no `dependencies=`,
and `grep -n 'CurrentSubject\\|CheckerDep\\|RawBearerToken' assist.py` returned nothing — the module
did not import `annotator.api.security` at all. Its siblings (projects, members, tasks,
project_events) all do.

WHAT IT IS AND IS NOT. This is not the flows situation, and the finding says so: there is no gateway
row for `/api/assist`, and the browser path gates first in `assist.remote.ts`. The reachable callers
are in-namespace pods — `chart/templates/network-policy.yaml` grants plain intra-namespace ingress —
and anyone port-forwarding. So it is defence in depth rather than an open edge. It still matters:
the POST reads a corpus unit and drives a model backend, spending GPU for a caller nobody checked,
with no audit row written.

WHY `can_annotate` ON THE PROJECT. An assist prediction is a PROPOSED WRITE — it comes back as
`status="prediction"`, `source="model:<name>"` rows the annotator renders and a reviewer accepts. Gating it on reading
would let anyone who may look at a corpus spend model compute against it and queue work for its reviewers.

The two GETs disclose the estate's model-backend topology (`/assist/producers`) and a task's ontology
(`/assist/generation-schema`), so they sit behind the same authenticated floor even though they touch
no unit — hence the router-level dependency, which is the reference's own rule: apply at the router
level when every route in the group needs it, "cheaper to read and harder to forget".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.assist import router
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, Task
from client_kit.media.deps import get_state
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import lakehouse_dir, write_table


def _app(*, allow: bool, record: list[dict[str, Any]] | None = None, subject: str = "gina") -> FastAPI:
    """An app whose FGA checker answers `allow` and records what it was asked."""

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    # The annotator's own settings, as its service builds them: no assist backend named, so the in-repo mock answers.
    state = AppState(settings=AnnotatorSettings())

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)  # ForbiddenError -> problem+json 403, as in the real service
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    # The VERIFIED subject. Overriding `current_subject` (not a header) is the point: with OIDC on
    # there is no header path to this value at all.
    app.dependency_overrides[current_subject] = lambda: subject
    app.dependency_overrides[get_state] = lambda: state
    return app


#: The corpus the gate resolves: its documents binding names the table the write rung is asked on.
CORPUS_DECLARED = {"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}, "document": {"table": "documents", "media_blob": "image"}}


@pytest.fixture(autouse=True)
def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The corpus, in a directory catalog the settings read, as the default dataset. The gate is decided before any unit
    is read, so the documents table holds no row."""
    documents = pa.table(
        {"doc_id": pa.array([], pa.string()), "image": lance.blob_array([])}, schema=pa.schema([pa.field("doc_id", pa.string()), lance.blob_field("image")])
    )
    write_table(tmp_path, ["acme", "corpus-1", "documents"], documents, descriptor=CORPUS_DECLARED)
    monkeypatch.setenv("WORKBENCH_SOURCES", json.dumps(lakehouse_dir(tmp_path)["WORKBENCH_SOURCES"]))
    monkeypatch.setenv("MEDIA_DEFAULT_DATASET", "lake~acme$corpus-1")


BODY = {"producer": "sam", "prompt": "a line of text"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/assist/doc-1/0/0?project=p1"),
        ("GET", "/api/assist/producers?project=p1"),
        ("GET", "/api/assist/generation-schema?task_id=t1&project=p1"),
    ],
)
def test_a_caller_without_the_relation_is_refused(method: str, path: str) -> None:
    """403 for a caller the checker denies — on all three routes, including the two GETs whose
    disclosure is topology rather than data."""
    client = TestClient(_app(allow=False))
    response = client.request(method, path, json=BODY if method == "POST" else None)
    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} for a denied caller — this route reads a corpus unit and spends model compute"
    )


def test_the_check_is_the_annotating_role_on_the_named_project() -> None:
    """A prediction is a proposed write, so the relation is `can_annotate`, on the project the request names."""
    asked: list[dict[str, Any]] = []
    client = TestClient(_app(allow=False, record=asked))
    client.post("/api/assist/doc-1/0/0?project=p1", json=BODY)
    assert asked, "no check was made at all"
    assert (asked[0]["user"], asked[0]["relation"], asked[0]["obj"]) == ("gina", "can_annotate", "project:p1")


def test_an_allowed_caller_gets_proposals_for_its_tasks_unit_named_by_the_datasets_own_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_app: Callable[..., FastAPI]
) -> None:
    """A unit is named by its dataset's identity, however many fields it has (here a document and a chunk), as the
    annotations doors name it, and must be one of the named task's item: the door answers the producer's proposals for
    it, and refuses a key the identity cannot name, a unit the task does not hold, no task, and a body past its bounds."""
    declared = {"identity": {"key_fields": ["doc_id", "chunk_id"]}, "document": {"table": "documents", "media_blob": "image"}}
    documents = pa.table(
        {"doc_id": pa.array([], pa.string()), "image": lance.blob_array([])}, schema=pa.schema([pa.field("doc_id", pa.string()), lance.blob_field("image")])
    )
    chunks = pa.table({"doc_id": pa.array(["doc-1", "doc-1"]), "chunk_id": pa.array([0, 1], pa.int64())})
    write_table(tmp_path, ["acme", "pages", "documents"], documents, descriptor=declared)
    write_table(tmp_path, ["acme", "pages", "chunks"], chunks, descriptor=declared)
    monkeypatch.setenv("MEDIA_DEFAULT_DATASET", "lake~acme$pages")
    app = state_app(router)

    async def allow(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        return True

    state = AppState(settings=AnnotatorSettings())
    app.dependency_overrides[get_checker] = lambda: allow
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_state] = lambda: state
    region = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}
    ask = {"producer": "grounding-dino", "prompt": "stamp", "region": region, "task_id": "t1"}
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="pages"))
        task = Task(task_id="t1", project_id="p1", source=ItemSource(kind="chunks", keys=["doc-1/0"]), media=MediaRef(kind="image"))
        client.portal.call(app.state.store.send_tasks, "p1", [task])

        answered = client.post("/api/assist/doc-1/0?project=p1", json=ask)
        refused = {
            "a-key-the-identity-cannot-name": client.post("/api/assist/doc-1/0/0?project=p1", json=ask),
            "a-unit-the-task-does-not-hold": client.post("/api/assist/doc-1/1?project=p1", json=ask),
            "no-task": client.post("/api/assist/doc-1/0?project=p1", json={**ask, "task_id": None}),
            "too-many-points": client.post("/api/assist/doc-1/0?project=p1", json={**ask, "points": [{"x": 1, "y": 1}] * 65}),
            "a-point-at-no-place": client.post(
                "/api/assist/doc-1/0?project=p1",
                content=json.dumps(ask)[:-1] + ', "points": [{"x": NaN, "y": 1}]}',
                headers={"content-type": "application/json"},
            ),
            "a-prompt-past-its-bound": client.post("/api/assist/doc-1/0?project=p1", json={**ask, "prompt": "x" * 2001}),
        }

    assert answered.status_code == 200, answered.text
    assert [(s["shape_type"], s["label"], s["x"], s["y"]) for s in answered.json()["shapes"]] == [("bbox", "stamp", 10.0, 20.0)]
    assert {name: r.status_code for name, r in refused.items()} == {
        "a-key-the-identity-cannot-name": 422,
        "a-unit-the-task-does-not-hold": 404,
        "no-task": 400,
        "too-many-points": 422,
        "a-point-at-no-place": 422,
        "a-prompt-past-its-bound": 422,
    }
    assert (
        refused["a-key-the-identity-cannot-name"].json()["detail"] == "unit key 'doc-1/0/0' has 3 part(s), and this dataset names a unit by 2: doc_id/chunk_id"
    )
    assert refused["a-unit-the-task-does-not-hold"].json()["detail"] == "unit doc-1/1 of dataset None is not this item's"
