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

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.assist import router
from client_kit.media.config import Settings
from client_kit.media.deps import get_state
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import write_table


def _app(*, allow: bool, record: list[dict[str, Any]] | None = None, subject: str = "gina") -> FastAPI:
    """An app whose FGA checker answers `allow` and records what it was asked."""

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    state = AppState(settings=Settings())

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
    monkeypatch.setenv("MEDIA_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_DEFAULT_DATASET", "acme$corpus-1")


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
