"""Any declared source lists its datasets at any depth, and a table with no workbench descriptor is read from its schema.

Driven through the real viewer app over HTTP on real pylance: a lakehouse stand-in (a directory catalog laid out as rask's),
a lance-namespace directory catalog with tables at its root and three namespaces deep, and a bare ``.lance`` table by
path (`workbench.sources`), the bare table marked annotatable and the directory catalog not, so each dataset says
whether it can be sent to a project and why not. The plain table is paged, sorted where Lance orders a column, searched where a full-text
index is, ranked where a vector index is, and refuses each of these where the table cannot answer it; a row's image cell
is named by its bytes and served through a signed media URL. Every read is bounded: a page starts no deeper than
`MAX_OFFSET`, a cell's text and list are cut at the read, a plain binary cell is never read, and a version, row or seed
row the table does not hold is a 404.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.media_catalog import LAKE, namespace_client, write_table
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app


_STAMP = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 4), "teal").save(out, format="PNG")
    return out.getvalue()


def _observations() -> pa.Table:
    notes = ["salt in the harbour", "iron to the works", "salt and grain", "letters from the governor"]
    return pa.table(
        {
            "n": pa.array([3, 1, 4, 2], pa.int64()),
            "note": notes,
            "tags": pa.array([["a"], ["b"], ["a", "b"], []], pa.list_(pa.utf8())),
            "embedding": pa.FixedSizeListArray.from_arrays(pa.array([1, 0, 0.9, 0.1, 0, 1, 0.1, 0.9], pa.float32()), 2),
        }
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    write_table(tmp_path / "lake", ["acme", "corpus", "chunks"], pa.table({"doc_id": ["d1"]}), descriptor=_STAMP)
    archive = tmp_path / "archive"
    write_table(archive, ["at_root"], pa.table({"x": [1]}))
    write_table(
        archive,
        ["wide"],
        pa.table({"text": ["t" * 70_000], "items": pa.array([list(range(200))], pa.list_(pa.int64())), "raw": pa.array([b"\x89PNG" * 10], pa.large_binary())}),
    )
    deep = write_table(archive, ["a", "b", "c", "observations"], _observations())
    deep.create_scalar_index("note", "INVERTED")
    lance.dataset(namespace_client=namespace_client(archive), table_id=["a", "b", "c", "observations"]).create_index(
        "embedding", "IVF_FLAT", num_partitions=1, metric="cosine"
    )
    images = pa.table(
        {"title": ["one", "two"], "image": lance.blob_array([_png(), b"plain bytes"])},
        schema=pa.schema([pa.field("title", pa.utf8()), lance.blob_field("image")]),
    )
    lance.write_dataset(images, str(tmp_path / "loose" / "scans.lance"), data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(pa.table({"y": [1]}), str(tmp_path / "loose" / "beside.lance"))
    # Beside the archive's root, outside every declared location: no id may reach it.
    lance.write_dataset(pa.table({"secret": ["outside"]}), str(tmp_path / "outside.lance"))
    key = tmp_path / "media-url-key"
    key.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    sources = [
        {"kind": "rask", "name": LAKE, "dir": str(tmp_path / "lake"), "identity": "none"},
        {"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(archive)}},
        {"kind": "tables", "name": "scans", "uri": str(tmp_path / "loose" / "scans.lance"), "annotatable": True},
    ]
    settings = ViewerSettings.model_validate({"WORKBENCH_SOURCES": sources, "VIEWER_MEDIA_URL_KEY_FILE": str(key)})
    app = create_viewer_app(settings)
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


ROWS = "/api/datasets/archive~a$b$c/tables/observations/rows"


UNMARKED = "source 'archive' is not marked annotatable"


def test_every_source_lists_its_datasets_whatever_their_depth(client: TestClient) -> None:
    listed = client.get("/api/datasets").json()["sources"]

    assert [(s["name"], s["kind"], [(d["id"], d["stamped"], sorted(d["tables"]), d["read_only"]) for d in s["datasets"]], s["unlisted"]) for s in listed] == [
        ("lake", "rask", [("lake~acme$corpus", True, ["chunks"], None)], []),
        ("archive", "namespace", [("archive~", False, ["at_root", "wide"], UNMARKED), ("archive~a$b$c", False, ["observations"], UNMARKED)], []),
        ("scans", "tables", [("scans~", False, ["scans"], None)], []),
    ]


def test_a_listed_table_whose_data_is_gone_is_answered_unavailable_once_a_while_and_hides_nothing_else(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The catalog still lists the table, its versions are gone (a table whose data was removed, as seen live): a dataset
    holding nothing else to read is unlisted with the reason and a 404 at its door, one holding other tables is listed
    with that table named, each said in ONE warning line with no traceback, and the next listing opens neither again."""
    archive = namespace_client(tmp_path / "archive")
    for table in (["wide"], ["a", "b", "c", "observations"]):
        uri = lance.dataset(namespace_client=archive, table_id=table).uri.removeprefix("file://")
        shutil.rmtree(Path(uri) / "_versions")
    caplog.set_level(logging.WARNING, logger="workbench.lancekit.registry")

    listings = [client.get("/api/datasets").json()["sources"] for _ in range(2)]
    door = client.get("/api/datasets/archive~a$b$c")

    gone = "the catalog lists it, but holds no version of it to read: declared and never written, or its data removed"
    archive_listing = [(s["datasets"], s["unlisted"]) for s in listings[0] if s["name"] == "archive"]
    assert listings[0] == listings[1]
    assert [(s["name"], [d["id"] for d in s["datasets"]]) for s in listings[0]] == [
        ("lake", ["lake~acme$corpus"]),
        ("archive", ["archive~"]),
        ("scans", ["scans~"]),
    ]
    assert [([(d["id"], sorted(d["tables"]), d["unreadable"]) for d in datasets], unlisted) for datasets, unlisted in archive_listing] == [
        ([("archive~", ["at_root"], {"wide": gone})], [{"id": "archive~a$b$c", "reason": gone}])
    ]
    assert (door.status_code, gone in door.json()["detail"]) == (404, True)
    assert sorted((r.getMessage(), r.exc_info) for r in caplog.records if r.name == "workbench.lancekit.registry") == [
        ("table observations of archive~a$b$c is listed but holds no version to read", None),
        ("table wide of archive~ is listed but holds no version to read", None),
    ]


def test_a_page_is_sorted_where_lance_orders_the_column(client: TestClient) -> None:
    page = client.get(ROWS, params={"sort": "n", "order": "desc", "limit": 2, "offset": 1}).json()

    assert ([c["name"] for c in page["columns"] if c["sortable"]], page["total"]) == (["n", "note"], 4)
    assert [(r["cells"]["n"], r["cells"]["tags"], r["cells"]["embedding"]["dim"]) for r in page["rows"]] == [(3, '["a"]', 2), (2, "[]", 2)]


@pytest.mark.parametrize(
    ("params", "status", "detail"),
    [
        pytest.param({"sort": "tags"}, 400, "holds json values, which Lance does not order", id="sort-a-list"),
        pytest.param({"q": "salt", "column": "n"}, 400, "needs a full-text index", id="search-an-unindexed-column"),
        pytest.param({"near": 0, "vector": "note"}, 400, "need a vector index", id="rank-by-a-text-column"),
    ],
)
def test_what_the_table_cannot_answer_is_refused_with_the_reason(client: TestClient, params: dict[str, object], status: int, detail: str) -> None:
    refused = client.get(ROWS, params=params)

    assert (refused.status_code, detail in refused.json()["detail"]) == (status, True)


def test_a_text_search_runs_where_a_full_text_index_is(client: TestClient) -> None:
    page = client.get(ROWS, params={"q": "salt"}).json()

    assert sorted(r["cells"]["note"] for r in page["rows"]) == ["salt and grain", "salt in the harbour"]


def test_similar_rows_are_ranked_by_the_indexed_vector_without_the_row_itself(client: TestClient) -> None:
    page = client.get(ROWS, params={"near": 0, "limit": 1}).json()

    assert [r["cells"]["note"] for r in page["rows"]] == ["iron to the works"]


def test_a_rows_image_is_named_by_its_bytes_and_served_through_a_signed_url(client: TestClient) -> None:
    page = client.get("/api/datasets/scans~/tables/scans/rows").json()
    rows = [client.get(f"/api/datasets/scans~/tables/scans/rows/{r['rowid']}", params={"version": page["version"]}).json() for r in page["rows"]]
    item = {"role": "cell", "table": page["qualified"], "column": "image", "row": rows[0]["rowid"], "version": page["version"]}
    [signed] = client.post("/api/signed-media", json={"items": [item]}).json()["urls"]
    served = client.get(str(signed["url"]))

    assert ([r["media"] for r in rows], page["qualified"]) == ([{"image": "image/png"}, {}], "scans~scans")
    assert (served.status_code, served.headers["content-type"], served.content) == (200, "image/png", _png())


def test_a_read_is_bounded_and_what_the_table_does_not_hold_is_not_found(client: TestClient) -> None:
    from viewer.services.plain_rows import DETAIL_CHARS, LIST_ITEMS, MAX_OFFSET

    wide = "/api/datasets/archive~/tables/wide/rows"
    page = client.get(wide).json()
    row = client.get(f"{wide}/{page['rows'][0]['rowid']}").json()
    misses = [
        client.get(wide, params={"offset": MAX_OFFSET + 1}).status_code,
        client.get(wide, params={"version": 99}).status_code,
        client.get(f"{wide}/99").status_code,
        client.get(ROWS, params={"near": 99}).status_code,
    ]

    assert (page["max_offset"], len(row["cells"]["text"]), len(json.loads(row["cells"]["items"])), row["cells"]["raw"]) == (
        MAX_OFFSET,
        DETAIL_CHARS + 1,
        LIST_ITEMS,
        {"size": None},
    )
    assert misses == [422, 404, 404, 404]


@pytest.mark.parametrize(
    ("method", "path", "body", "detail"),
    [
        pytest.param("get", "/api/datasets/archive~..", None, "invalid dataset id 'archive~..': id segment '..' names no table or namespace", id="dataset"),
        pytest.param("get", "/api/datasets/archive~/tables/..%5Coutside/rows", None, "missing from dataset 'archive~'", id="rows"),
        pytest.param("get", "/api/pages", {"table": "archive~..$outside"}, "no table 'archive~..$outside': id segment '..' names no table", id="pages"),
        pytest.param("get", "/api/pages", {"table": "archive~outside"}, "table 'outside' is not one source 'archive' lists", id="pages-unlisted"),
        pytest.param("post", "/api/signed-media", {"table": "archive~../outside"}, "id segment '../outside' holds a path or control character", id="cell"),
    ],
)
def test_no_id_reaches_a_table_outside_the_declared_locations(client: TestClient, method: str, path: str, body: dict[str, object] | None, detail: str) -> None:
    """A sibling ``outside.lance`` lies beside the archive's root: no id a request can send opens it."""
    if method == "post":
        answer = client.post(path, json={"items": [{"role": "cell", "column": "secret", "row": 0, "version": 1, **(body or {})}]})
        status, said = answer.status_code, answer.json()["urls"][0]["detail"]
    else:
        answer = client.get(path, params=body)
        status, said = answer.status_code, answer.json()["detail"]

    assert (status, detail in said) == (200 if method == "post" else 404, True), said
