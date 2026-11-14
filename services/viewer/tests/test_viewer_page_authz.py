"""The page BYTES stop being public (#90).

`GET /api/page` streamed a bronze page image to anyone who could reach the gateway, and `/api/pages`
listed the volume's contents to anyone. Neither declared an auth dependency. Its neighbour
`datasets.py` was already gated, which made this an inconsistency rather than a stance.

For an archive the consequence is not abstract: these routes are the primary read path for scanned
records, and the estate holds sealed material, donor-restricted collections and records naming living
people. Every rail the catalog grew — protection flags, trash/undrop, tiered credential vending —
governs the TABLE while this route handed out the pixels.

Two rungs, not one. `/api/pages` lists metadata and takes `can_get_metadata`; `/api/page` returns
image bytes and takes `can_read_data`. The model already separates them, and for an archive the
difference is the point: knowing that a volume of sealed records exists is a different permission
from reading it.

The third defect fixed here is the bearer. The table was resolved at the catalog with NO Authorization header at all —
so on an auth-enabled catalog it 401'd, and where it succeeded it succeeded as nobody in particular. The caller's bearer
already arrives at the viewer (sealed cookie -> zone BFF -> gateway); it rides the namespace client the table opens
through, observed at a real socket because that client is pylance's Rust REST client.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance import blob_array, blob_field

from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import register_handlers
from tests.media_catalog import write_table
from viewer.api.security import READ_DATA, READ_METADATA
from viewer.api.v1.endpoints import pages as pg
from viewer.api.v1.endpoints.pages import router
from viewer.core.config import ViewerSettings, get_viewer_settings


TABLE: str = "acme$bronze$pages"

#: REAL magic bytes, not the string "JPEGBYTES". The route sniffs the payload instead of asserting `image/jpeg` over
#: whatever it was handed (open_fastapi-audit), so a fixture that is not actually a JPEG would correctly be served as
#: `application/octet-stream` — and the content-type assertion below would be testing the fixture rather than the route.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"


def _app(settings: Settings, *, allow: Any, seen: list[dict[str, Any]] | None = None, subject: str = "dex~gina", token: str | None = "caller-jwt") -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow if isinstance(allow, bool) else obj in allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    viewer_settings = ViewerSettings.model_validate(
        {
            "WORKBENCH_FGA_ENABLED": True,
            "WORKBENCH_OIDC_ENABLED": True,
            "WORKBENCH_OIDC_ISSUER": "https://issuer.test",
            "WORKBENCH_OIDC_AUDIENCE": "rask",
            "WORKBENCH_OIDC_CONNECTORS": {"local": "dex"},
            **({"MEDIA_CATALOG_URI": settings.catalog_uri} if settings.catalog_uri else {"MEDIA_CATALOG_DIR": str(settings.catalog_dir)}),
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: viewer_settings
    app.dependency_overrides[pg.CheckerDep.__metadata__[0].dependency] = lambda: checker
    app.dependency_overrides[pg.CurrentSubject.__metadata__[0].dependency] = lambda: subject
    app.dependency_overrides[pg.RawBearerToken.__metadata__[0].dependency] = lambda: token
    state = AppState(settings=settings)
    app.dependency_overrides[pg.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture
def pages(tmp_path: Path) -> Settings:
    """A REAL tiny blob-v2 page table in a directory catalog, read through Lance's own take-path
    (`id -> _rowid -> take_blobs`): a double of an internal seam dies silently the moment the read path moves. The
    governed-tier columns the LISTING projects must exist, or the listing fails on schema rather than deciding
    authorization — which is what this file is for."""
    table = pa.table(
        {
            "id": pa.array([0, 1], pa.int64()),
            "source_uri": pa.array(["iiif://vol/0", "iiif://vol/1"], pa.string()),
            "stage": pa.array(["bronze", "bronze"], pa.string()),
            "payload": blob_array([_JPEG, None]),
        },
        schema=pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("stage", pa.string()), blob_field("payload")]),
    )
    write_table(tmp_path, TABLE.split("$"), table)
    return Settings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)})


class _Catalog:
    """A catalog's REST door on a real socket: it records the bearer of each DescribeTable and answers ``status``."""

    def __init__(self) -> None:
        self.bearers: list[str | None] = []
        self.status = 404
        catalog = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the stdlib's handler name
                catalog.bearers.append(self.headers.get("Authorization"))
                body = json.dumps({"error": "refused", "code": {401: 16, 404: 4}[catalog.status]}).encode()
                self.send_response(catalog.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Catalog]:
    stub = _Catalog()
    threading.Thread(target=stub._server.serve_forever, daemon=True).start()
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    yield stub
    stub._server.shutdown()


# --- the gate ---------------------------------------------------------------------------------


def test_page_BYTES_are_refused_without_a_grant(pages: Settings) -> None:
    """The defect, stated directly: this used to return the image to anyone."""
    client = TestClient(_app(pages, allow=False))

    r = client.get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 403
    assert _JPEG not in r.content


def test_the_page_LISTING_is_refused_without_a_grant(pages: Settings) -> None:
    """A volume's contents name records someone may not know exist."""
    client = TestClient(_app(pages, allow=False))

    assert client.get("/api/pages", params={"table": TABLE}).status_code == 403


def test_bytes_need_can_read_data_while_the_listing_needs_only_metadata(pages: Settings) -> None:
    """The two rungs are DIFFERENT, and that difference is the archival point.

    A caller granted metadata may see that a volume of sealed records exists and how many pages it
    has, and still be refused the pixels. Collapsing both onto one relation would silently widen
    whichever surface got the weaker one.
    """
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(pages, allow=True, seen=seen))

    listing = client.get("/api/pages", params={"table": TABLE})
    page = client.get("/api/page", params={"table": TABLE, "id": 0})

    assert (listing.status_code, page.status_code, page.content) == (200, 200, _JPEG)
    assert [s["relation"] for s in seen] == [READ_METADATA, READ_DATA]
    assert {s["obj"] for s in seen} == {f"table:{TABLE}"}


# --- the bearer -------------------------------------------------------------------------------


def test_the_CALLERS_bearer_is_forwarded_to_the_catalog(catalog: _Catalog) -> None:
    """Not a service credential, and not nothing.

    `raw_bearer`'s own docstring says why a service token is wrong: the catalog checks one relation
    on one `table:` object and injects no row predicate, so a service identity answers 200 for a
    caller with no grant at all. Before this, the header was absent entirely.
    """
    client = TestClient(_app(Settings.model_validate({"MEDIA_CATALOG_URI": catalog.url}), allow=True, token="caller-jwt"))

    client.get("/api/page", params={"table": TABLE, "id": 0})

    assert catalog.bearers == ["Bearer caller-jwt"]


def test_a_catalog_401_says_WHICH_credential_problem_it_was(catalog: _Catalog) -> None:
    """ "No credential sent" and "credential refused" are different problems with different fixes: "the caller is
    anonymous" and "the caller was refused" send a reader in opposite directions."""
    catalog.status = 401
    settings = Settings.model_validate({"MEDIA_CATALOG_URI": catalog.url})

    anon = TestClient(_app(settings, allow=True, token=None)).get("/api/page", params={"table": TABLE, "id": 0})
    held = TestClient(_app(settings, allow=True, token="caller-jwt")).get("/api/page", params={"table": TABLE, "id": 0})

    assert (anon.status_code, held.status_code) == (401, 401)
    assert "no bearer was sent" in anon.text
    assert "bearer was refused" in held.text
