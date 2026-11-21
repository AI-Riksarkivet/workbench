"""The atlas projection (`GET /api/atlas/points`) is one payload per (dataset, space, table version), shared by every
caller, and so reached only through each caller's own read.

- A browser keeps it and revalidates it by its ETag; while the version stands the answer is a 304 with no body.
- Callers asking for the same cold payload at once share one build (a full-table scan and encode), and its failure,
  which is not cached.
- A caller the catalog refuses gets the refusal, never the payload another caller warmed. Driven against a REST catalog on
  a real socket that answers per bearer (``tests.vending_catalog``).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.media_catalog import lakehouse_dir, lakehouse_rest, write_table
from tests.vending_catalog import VendingCatalog
from viewer.api import security
from viewer.api.v1.endpoints import atlas
from viewer.core.config import ViewerSettings
from viewer.main import create_viewer_app
from workbench.exceptions import ServiceUnavailableError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.descriptor import AtlasSpace, Declared


_DESCRIPTOR = {
    "identity": {"key_fields": ["doc_id"]},
    "atlas": [{"name": "text", "x": "x", "y": "y", "cluster": "cluster", "source_column": "text", "table": "chunks"}],
}
_ROWS = pa.table(
    {
        "doc_id": ["d1", "d2"],
        "text": ["a", "b"],
        "x": pa.array([0.5, 1.5], pa.float32()),
        "y": pa.array([2.5, 3.5], pa.float32()),
        "cluster": pa.array([0, 1], pa.int32()),
    }
)


def _app(root: Path) -> FastAPI:
    return create_viewer_app(ViewerSettings.model_validate({**lakehouse_dir(str(root))}))


def test_a_payload_the_browser_holds_is_revalidated_not_sent_again(tmp_path: Path) -> None:
    write_table(tmp_path, ["wh", "corpus", "chunks"], _ROWS, descriptor=_DESCRIPTOR)

    with TestClient(_app(tmp_path)) as client:
        first = client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"})
        again = client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"}, headers={"If-None-Match": first.headers["etag"]})

    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "private, no-cache"
    assert (again.status_code, again.content, again.headers["etag"]) == (304, b"", first.headers["etag"])


def test_callers_asking_for_one_cold_payload_at_once_share_one_build_and_its_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_table(tmp_path, ["wh", "corpus", "chunks"], _ROWS, descriptor=_DESCRIPTOR)
    builds: list[int] = []
    real = atlas.build_points

    def _failing_build(declared: Declared, space: AtlasSpace, ds: lance.LanceDataset) -> bytes:
        builds.append(1)
        time.sleep(0.5)
        if len(builds) == 1:
            raise ServiceUnavailableError("the scan failed")
        return real(declared, space, ds)

    monkeypatch.setattr(atlas, "build_points", _failing_build)
    with TestClient(_app(tmp_path)) as client, ThreadPoolExecutor(max_workers=8) as pool:
        failed = list(pool.map(lambda _: client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"}).status_code, range(8)))
        builds_while_failing = len(builds)
        later = client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"})

    assert failed == [503] * 8
    assert builds_while_failing == 1
    assert (later.status_code, len(builds)) == (200, 2)


def test_a_refused_caller_is_not_served_the_payload_another_caller_warmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    location = write_table(tmp_path, ["wh", "corpus", "chunks"], _ROWS, descriptor=_DESCRIPTOR).uri
    catalog = VendingCatalog(location, {"caller-a": {}, "caller-b": "refuse"})
    app = create_viewer_app(ViewerSettings.model_validate({**lakehouse_rest(catalog.url)}))
    as_whom = {"token": "caller-a"}
    app.dependency_overrides[security._deps.lakehouse_caller] = lambda: LakehouseCaller(subject=f"keycloak~{as_whom['token']}", token=lambda: as_whom["token"])
    try:
        with TestClient(app) as client:
            warmed = client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"})
            as_whom["token"] = "caller-b"
            refused = client.get("/api/atlas/points", params={"dataset": "lake~wh$corpus"})
    finally:
        catalog.close()

    assert warmed.status_code == 200, warmed.text
    assert refused.status_code == 403, refused.text
    assert refused.headers["content-type"] == "application/problem+json"
