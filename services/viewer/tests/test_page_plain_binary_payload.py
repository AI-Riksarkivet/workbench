"""A plain ``large_binary`` payload column is served, not 500'd — both routes, one answer.

Adversarial re-audit of VS-05's fix (8834b95a): ``table`` is a free caller-supplied catalog id, so a
``payload`` that is a plain ``large_binary`` column rather than blob-v2 is a reachable shape — and
`_page_rows` says exactly that in its own docstring, branching the LISTING on
``is_blob_field(ds.schema.field("payload"))``. But `_take_page` called
``take_blobs("payload", ids=[rowid])`` unconditionally, which raises on a non-blob column: the same
table answered 200 on ``/api/pages`` and 500 on ``/api/page``. A listing that advertises pages the
byte route then refuses to serve is worse than either route failing alone.

The listing branch is here too, and what it must NOT do is the other half of VS-05: this shape carries
no blob descriptor, so the only place its per-row length and validity exist is the payload itself, and
reading that to answer a ``can_get_metadata`` call is the corpus-sized read the finding is about. It
therefore reports both unknown; the byte route below stays the authority on presence.

A REAL dataset rather than a mock, for the reason the sibling suites give: the branch condition is a
measured pylance behaviour (a scan strips the Arrow extension marker, so the shape question must be
asked of ``ds.schema``), and a double would restate the assumption instead of testing it.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client_kit.media.config import MediaSettings
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from service_kit.governed.token_exchange import LakehouseCaller
from tests.media_catalog import namespace_client, write_table
from viewer.api.v1.endpoints import pages as pg
from viewer.api.v1.endpoints.pages import router
from viewer.core.config import ViewerSettings, get_viewer_settings


TABLE: str = "lake~acme$bronze$pages"

#: Real JPEG magic so the sniff is exercised on the fixture — a payload that were not actually a
#: JPEG would honestly be served as `application/octet-stream` and the content-type assertion below
#: would be testing the fixture rather than the route.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x2a" * 500

#: The id whose payload is null — a registered row that failed to acquire, sitting in the same
#: dataset as the real one so the absence signal is read from among present rows.
NULL_ROW_ID = 1


@pytest.fixture(scope="module")
def dataset_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bronze-shaped dataset whose ``payload`` is a PLAIN ``large_binary`` column, not blob-v2."""
    table = pa.table(
        {
            "id": pa.array([0, NULL_ROW_ID], pa.int64()),
            "source_uri": pa.array(["iiif://vol/0", "iiif://vol/1"], pa.string()),
            "stage": pa.array(["bronze", "bronze"], pa.string()),
            "payload": pa.array([_JPEG, None], pa.large_binary()),
        }
    )
    root = tmp_path_factory.mktemp("plainpages")
    write_table(root, TABLE.split("$"), table)
    return root


def _app() -> FastAPI:
    """The pages router with the authorization plane satisfied — this file tests the SHAPE branch.

    The gate itself is pinned by `services/viewer/tests/test_viewer_page_authz.py`; granting here keeps a
    permission failure from masquerading as a shape failure.
    """

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    settings = ViewerSettings()
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    app.dependency_overrides[pg.CallerDep.__metadata__[0].dependency] = lambda: LakehouseCaller(subject="keycloak~gina", token=lambda: "caller-jwt")
    state = AppState(settings=MediaSettings())
    app.dependency_overrides[pg.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture
def _plain_dataset(dataset_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = lance.dataset(namespace_client=namespace_client(dataset_path), table_id=TABLE.split("$"))
    monkeypatch.setattr(pg.lance, "dataset", lambda *_a, **_kw: real)


@pytest.mark.usefixtures("_plain_dataset")
def test_the_bytes_route_serves_a_plain_binary_payload() -> None:
    """The defect, stated directly: the listing 200'd for this table while `/api/page` 500'd on the
    unconditional `take_blobs` of a column that is not a blob column."""
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 200
    assert r.content == _JPEG
    assert r.headers["content-type"] == "image/jpeg", "the sniff must run on the plain shape too"
    # Read as the person, so kept by their own browser alone and keyed on the token it was read with.
    assert (r.headers["cache-control"], r.headers["vary"]) == ("private, max-age=300", "Authorization")


@pytest.mark.usefixtures("_plain_dataset")
def test_a_null_plain_payload_is_the_same_404_as_a_null_blob() -> None:
    """The absence signal must not depend on the column's physical shape."""
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": NULL_ROW_ID})

    assert r.status_code == 404
    assert "has no payload" in r.text


@pytest.mark.usefixtures("_plain_dataset")
def test_an_absent_id_is_a_404_on_the_plain_shape_too() -> None:
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": 999})

    assert r.status_code == 404
    assert "no page with id 999" in r.text
