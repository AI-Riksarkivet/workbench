"""A table with no workbench descriptor feeds the Atlas, the Graph and the Tree from the columns a person picks.

Driven through the real viewer app over HTTP on real pylance tables in a lance-namespace directory catalog
(`viewer.services.derived_views`, `viewer.api.v1.endpoints.derived_views`). An atlas places each row by two numeric
columns or projects a vector column, a graph links an edge table's ends or a self-referencing table's rows, and a tree
nests rows by their parent or their path. A node's rows are the rows the node was counted over, read by the same
definition. Every read is bounded and says so: a table past the cap is read as an evenly spaced sample, a cell is cut at
the read, a graph keeps its heaviest edges, a tree its largest branches to a fixed depth, non-finite numbers are left
out and counted, the deadline cuts a read and stops the work on it, cold builds take a slot or are refused, one cold
answer is built once, and an answer larger than the cache's budget is not kept. A column a request names is looked up
in the schema before anything is read.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from tests.media_catalog import namespace_client, write_table
from viewer.api.v1.endpoints import derived_views as doors
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app
from viewer.services import derived_views
from viewer.services.derived_views import AtlasSpec, Budget
from viewer.services.plain_rows import ColumnView


BASE = "/api/datasets/archive~field/tables"


def _points() -> pa.Table:
    """Ten rows on a line, in two kinds; their vectors lie in two clusters along the first axis."""
    vectors = [[10.0 + i * 0.1, 0.2 * (i % 2), 0.0] if i < 5 else [-10.0 - i * 0.1, 0.2 * (i % 2), 0.0] for i in range(10)]
    return pa.table(
        {
            "name": [f"p{i}" for i in range(10)],
            "kind": ["a"] * 5 + ["b"] * 5,
            "east": pa.array([float(i * 1000) for i in range(10)]),
            "north": pa.array([float(-i) for i in range(10)]),
            "heat": pa.array([None, *[float(i) for i in range(1, 30, 3)]][:10]),
            "embedding": pa.FixedSizeListArray.from_arrays(pa.array([v for row in vectors for v in row], pa.float32()), 3),
        }
    )


def _links() -> pa.Table:
    """An edge table: a heavy pair, a light pair, a self-loop, a row with no target and a sink no row starts from."""
    return pa.table(
        {
            "src": ["ada", "ada", "ada", "bo", "cy", "dee", "eve"],
            "dst": ["bo", "bo", "cy", "bo", None, "zed", "ada"],
            "rel": ["knows", "knows", "met", "self", "lost", "met", "knows"],
            "w": [5.0, 5.0, 1.0, 1.0, 1.0, 0.5, 0.25],
        }
    )


def _org() -> pa.Table:
    """A parent tree with an orphan (its parent is no row) and a two-node cycle no root reaches; and a path per row."""
    return pa.table(
        {
            "id": pa.array([1, 2, 3, 4, 5, 6, 7], pa.int64()),
            "parent": pa.array([None, 1, 1, 2, 99, 7, 6], pa.int64()),
            "title": ["root", "left", "right", "leaf", "orphan", "loop-a", "loop-b"],
            "path": ["a/b/c", "a/b/d", "a/e", "a/e", "f", None, "f/g"],
            "n": pa.array([1, 2, 3, 4, 5, 6, 7], pa.int64()),
        }
    )


def _odd() -> pa.Table:
    """Non-finite numbers where a weight, a size and a position are read, and an integer and a text id that read alike."""
    nan, inf = float("nan"), float("inf")
    return pa.table(
        {
            "src": ["a", "b", "c", "a"],
            "dst": ["b", "c", "a", "c"],
            "w": [1.0, nan, 2.0, inf],
            "path": ["x/y", "x/z", "q", "x/y"],
            "size": [1.0, inf, 2.0, 3.0],
            "east": [1.0, nan, 3.0, -inf],
            "north": [1.0, 2.0, 3.0, 4.0],
            "far": [-1e308, 1e308, 0.0, 1e308],
            "num": pa.array([1, 2, 3, 1], pa.int64()),
            "text": ["1", "2", "3", "9"],
        }
    )


#: Paths a person writes: a leading, a trailing and a doubled separator, levels that are empty, letters past ASCII.
PATHS = ["/usr/lib/x", "/usr/lib/y", "/usr/bin/z", "a//b", "a/b/", "//", "å/ä/ö", "å/ä", "/usr/lib/x"]


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    archive = tmp_path / "archive"
    tables = {
        "points": _points(),
        "links": _links(),
        "org": _org(),
        "odd": _odd(),
        "paths": pa.table({"path": PATHS}),
        "empty": pa.schema([("a", pa.float64()), ("b", pa.float64()), ("s", pa.string()), ("t", pa.string())]).empty_table(),
        "times": pa.table({"t": pa.array([1, 2], pa.time32("s")), "d": pa.array([1, 2], pa.duration("s")), "s": ["a", "b"]}),
    }
    for name, data in tables.items():
        write_table(archive, ["field", name], data)
    opened: list[TestClient] = []

    def make(**extra: object) -> TestClient:
        settings = ViewerSettings.model_validate(
            {"WORKBENCH_SOURCES": [{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(archive)}}], **extra}
        )
        app = create_viewer_app(settings)
        app.dependency_overrides[get_viewer_settings] = lambda: settings
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def _stream(body: bytes) -> pa.Table:
    with pa.ipc.open_stream(body) as reader:
        return reader.read_all()


def _nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        found.append(node)
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return found


def test_an_atlas_places_each_row_by_the_columns_picked_and_says_what_it_sampled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    picks = {"x": "east", "y": "north", "color": "kind", "label": "name"}
    whole = _stream(client.get(f"{BASE}/points/atlas", params=picks).content)
    monkeypatch.setattr(derived_views, "ATLAS_POINTS", 4)
    sampled = _stream(client.get(f"{BASE}/points/atlas", params={**picks, "label": "kind"}).content)
    monkeypatch.setattr(derived_views, "CATEGORY_LIKE", 2)
    heat = _stream(client.get(f"{BASE}/points/atlas", params={"x": "east", "y": "north", "color": "heat"}).content)

    assert (whole.column("x").to_pylist()[0], whole.column("x").to_pylist()[-1], whole.column("y").to_pylist()[0]) == (-1.0, 1.0, 1.0)
    assert whole.column("rowid").to_pylist() == list(range(10))
    assert (whole.column("color").to_pylist()[4:6], whole.column("label").to_pylist()[2]) == (["a", "b"], "p2")
    assert {k.decode(): v.decode() for k, v in (whole.schema.metadata or {}).items()} == {
        "count": "10",
        "total": "10",
        "read": "10",
        "dropped": "0",
        "sampled": "false",
        "method": "columns",
        "color_kind": "category",
    }
    assert (sampled.column("rowid").to_pylist(), (sampled.schema.metadata or {})[b"sampled"]) == ([0, 2, 5, 7], b"true")
    # A numeric colour's missing value is no value, never a 0.
    assert [str(v) for v in heat.column("color").to_pylist()[:2]] == ["nan", "4.0"]


def test_a_vector_column_is_projected_onto_its_principal_components_the_same_way_every_time(client: TestClient) -> None:
    first = client.get(f"{BASE}/points/atlas", params={"vector": "embedding", "color": "kind"})
    again = client.get(f"{BASE}/points/atlas", params={"vector": "embedding", "color": "kind"})
    points = _stream(first.content)
    xs = points.column("x").to_pylist()
    meta = points.schema.metadata or {}

    assert (first.status_code, first.content == again.content) == (200, True)
    assert (meta[b"method"], float(meta[b"variance"]) > 0.99) == (b"pca", True)
    # The two clusters fall on either side of the first component, each kind on its own side.
    assert {k: {x > 0 for x, kind in zip(xs, points.column("color").to_pylist(), strict=True) if kind == k} for k in ("a", "b")} in (
        {"a": {True}, "b": {False}},
        {"a": {False}, "b": {True}},
    )


def test_an_edge_table_is_a_graph_of_its_heaviest_edges_with_what_was_left_out(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derived_views, "GRAPH_EDGES", 2)
    graph = client.get(f"{BASE}/links/graph", params={"shape": "edges", "source": "src", "target": "dst", "relation": "rel", "weight": "w"}).json()

    assert [(e["source"], e["target"], e["description"]) for e in graph["edges"]] == [("s:ada", "s:bo", "knows"), ("s:ada", "s:cy", "met")]
    assert sorted((n["id"], n["name"], n["mentions"], n["rows"]) for n in graph["nodes"]) == [
        ("s:ada", "ada", 11, 4),
        ("s:bo", "bo", 10, 3),
        ("s:cy", "cy", 1, 1),
    ]
    assert graph["diagnostics"] == {
        "extent": {"total": 7, "read": 7, "sampled": False, "cut": None},
        "nodes": 6,
        "edges": 5,
        "null_rows": 1,
        "dropped_weights": 0,
        "self_loops": 1,
        "unmatched_targets": 1,
        "mixed_types": False,
        "clipped_keys": 0,
        "cut_nodes": 3,
        "cut_edges": 3,
    }


def test_a_self_referencing_table_is_a_graph_of_its_rows_named_and_coloured_by_their_columns(client: TestClient) -> None:
    graph = client.get(f"{BASE}/org/graph", params={"shape": "self", "source": "id", "target": "parent", "label": "title", "group": "path"}).json()
    named = {n["id"]: (n["name"], n["type"]) for n in graph["nodes"]}

    assert (named["i:1"], named["i:4"], len(named)) == (("root", "a/b/c"), ("leaf", "a/e"), 7)
    assert sorted((e["source"], e["target"]) for e in graph["edges"]) == [("i:2", "i:1"), ("i:3", "i:1"), ("i:4", "i:2"), ("i:6", "i:7"), ("i:7", "i:6")]
    assert (graph["diagnostics"]["unmatched_targets"], graph["diagnostics"]["edges"], graph["diagnostics"]["null_rows"]) == (1, 5, 0)


def test_an_integer_and_a_text_id_that_read_alike_are_different_nodes_and_the_mix_is_flagged(client: TestClient) -> None:
    graph = client.get(f"{BASE}/odd/graph", params={"shape": "edges", "source": "num", "target": "text"}).json()

    assert sorted(n["id"] for n in graph["nodes"]) == ["i:1", "i:2", "i:3", "s:1", "s:2", "s:3", "s:9"]
    assert graph["diagnostics"]["mixed_types"] is True


def test_a_parent_tree_nests_its_rows_and_counts_its_orphans_and_cycles(client: TestClient) -> None:
    tree = client.get(f"{BASE}/org/tree", params={"shape": "parent", "node": "id", "parent": "parent", "label": "title", "size": "n"}).json()
    root = tree["hierarchy"]

    assert [(c["name"], json.loads(c["id"]), c["rows"]) for c in root["children"]] == [("root", {"id": "i:1"}, 4), ("orphan", {"id": "i:5"}, 1)]
    assert [(c["name"], c.get("value"), c["rows"]) for c in root["children"][0]["children"]] == [
        ("left", None, 2),
        ("right", 3.0, 1),
        ("root (itself)", 1.0, 1),
    ]
    assert tree["diagnostics"] == {
        "extent": {"total": 7, "read": 7, "sampled": False, "cut": None},
        "nodes": 7,
        "roots": 1,
        "orphans": 1,
        "cycles": 2,
        "null_rows": 0,
        "dropped_sizes": 0,
        "clipped": 0,
        "depth": 3,
        "cut_nodes": 0,
    }


def test_a_path_tree_skips_empty_levels_and_a_nodes_rows_are_the_rows_it_counted(client: TestClient) -> None:
    tree = client.get(f"{BASE}/paths/tree", params={"shape": "path", "node": "path", "separator": "/"}).json()
    nodes = [n for n in _nodes(tree["hierarchy"]) if "id" in n]
    listed = {
        n["id"]: client.get(f"{BASE}/paths/tree/rows", params={"shape": "path", "node": "path", "separator": "/", "key": n["id"], "limit": 200}).json()
        for n in nodes
    }

    def levels(key: str) -> tuple[str, ...]:
        return tuple(json.loads(key)["path"])

    assert sorted(levels(n["id"]) for n in nodes if not json.loads(n["id"]).get("alone")) == [
        ("a",),
        ("a", "b"),
        ("usr",),
        ("usr", "bin"),
        ("usr", "bin", "z"),
        ("usr", "lib"),
        ("usr", "lib", "x"),
        ("usr", "lib", "y"),
        ("å",),
        ("å", "ä"),
        ("å", "ä", "ö"),
    ]
    assert {key: (page["total"], len(page["rows"])) for key, page in listed.items()} == {n["id"]: (n["rows"], n["rows"]) for n in nodes}
    assert sorted(r["cells"]["path"] for r in listed[json.dumps({"path": ["usr", "lib"]}, separators=(",", ":"))]["rows"]) == [
        "/usr/lib/x",
        "/usr/lib/x",
        "/usr/lib/y",
    ]
    assert (tree["hierarchy"]["rows"], tree["diagnostics"]["null_rows"]) == (8, 1)


def test_every_node_of_a_parent_tree_and_a_graph_lists_the_rows_it_counted(client: TestClient) -> None:
    spec = {"shape": "parent", "node": "id", "parent": "parent"}
    tree = client.get(f"{BASE}/org/tree", params=spec).json()
    trees = {n["id"]: n["rows"] for n in _nodes(tree["hierarchy"]) if "id" in n}
    tree_rows = {key: client.get(f"{BASE}/org/tree/rows", params={**spec, "key": key}).json()["total"] for key in trees}
    edges = {"shape": "edges", "source": "src", "target": "dst", "weight": "w"}
    graph = client.get(f"{BASE}/links/graph", params=edges).json()
    graph_rows = {n["id"]: client.get(f"{BASE}/links/graph/rows", params={**edges, "key": n["id"]}).json() for n in graph["nodes"]}

    assert (tree_rows, len(trees)) == (trees, 7)
    # Every node, one only ever pointed at ("zed", "bo" here) as much as one rows start from, is named by rows: its
    # count, its first row and its listing are the same rows.
    listed = {n["id"]: (n["rows"], n["first"] in [r["rowid"] for r in graph_rows[n["id"]]["rows"]]) for n in graph["nodes"]}
    assert listed == {key: (page["total"], True) for key, page in graph_rows.items()}
    assert sorted(r["cells"]["dst"] for r in graph_rows["s:bo"]["rows"]) == ["bo", "bo", "bo"]


def test_a_non_finite_weight_size_or_position_is_left_out_counted_and_every_answer_is_strict_json(client: TestClient) -> None:
    graph = client.get(f"{BASE}/odd/graph", params={"shape": "edges", "source": "src", "target": "dst", "weight": "w"})
    tree = client.get(f"{BASE}/odd/tree", params={"shape": "path", "node": "path", "size": "size"})
    atlas = _stream(client.get(f"{BASE}/odd/atlas", params={"x": "east", "y": "north"}).content)
    far = _stream(client.get(f"{BASE}/odd/atlas", params={"x": "far", "y": "north"}).content)

    for answer in (graph, tree):
        assert answer.status_code == 200, answer.text
        json.loads(answer.text, parse_constant=lambda name: pytest.fail(f"{name} in a JSON answer"))
    assert (graph.json()["diagnostics"]["dropped_weights"], len(graph.json()["edges"])) == (2, 2)
    assert (tree.json()["diagnostics"]["dropped_sizes"], tree.json()["hierarchy"]["rows"]) == (1, 3)
    assert ((atlas.schema.metadata or {})[b"dropped"], atlas.column("rowid").to_pylist()) == (b"2", [0, 2])
    # Positions near the largest float are scaled without overflowing their span.
    assert far.column("x").to_pylist() == [-1.0, 1.0, 0.0, 1.0]


def test_a_huge_cell_is_cut_at_the_read_and_a_deep_path_at_the_trees_depth(make_client: Callable[..., TestClient], tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    write_table(archive, ["field", "deep"], pa.table({"path": ["/".join(["a"] * 40_000)]}))
    write_table(
        archive, ["field", "long"], pa.table({"src": [f"{i:04d}" + "x" * 20_000 for i in range(50)], "dst": [f"{i:04d}" + "y" * 20_000 for i in range(50)]})
    )
    client = make_client()

    tree = client.get(f"{BASE}/deep/tree", params={"shape": "path", "node": "path"})
    graph = client.get(f"{BASE}/long/graph", params={"shape": "edges", "source": "src", "target": "dst"})

    assert (tree.status_code, tree.json()["diagnostics"]["depth"], tree.json()["diagnostics"]["clipped"]) == (200, derived_views.TREE_DEPTH, 1)
    assert len(tree.content) < 64 * 1024
    assert (graph.status_code, graph.json()["diagnostics"]["clipped_keys"]) == (200, 100)
    assert {len(n["id"]) for n in graph.json()["nodes"]} == {derived_views.KEY_CHARS + 2}


@pytest.mark.parametrize(
    ("table", "read", "picks", "status", "detail"),
    [
        pytest.param("points", "atlas", {"x": "east` OR 1=1 --", "y": "north"}, 400, "no column 'east` OR 1=1 --'", id="a-name-the-schema-lacks"),
        pytest.param("points", "atlas", {"x": "name", "y": "north"}, 400, "holds text values; the x position reads", id="text-as-a-position"),
        pytest.param("points", "atlas", {"x": "east", "y": "north", "vector": "embedding"}, 400, "not both", id="two-placements"),
        pytest.param("points", "atlas", {"x": "east", "y": "east"}, 400, "must be different columns", id="one-column-twice"),
        pytest.param("points", "atlas", {"vector": "east"}, 400, "holds float values; the position reads vector", id="a-number-as-a-vector"),
        pytest.param("links", "graph", {"shape": "edges", "source": "src", "target": "w"}, 400, "holds float values; the to reads", id="a-number-as-a-node"),
        pytest.param("links", "graph", {"shape": "self", "source": "src", "target": "src"}, 400, "must be different columns", id="an-id-pointing-at-itself"),
        pytest.param("org", "tree", {"shape": "parent", "node": "id"}, 400, "needs the column that names each row's parent", id="no-parent"),
        pytest.param("org", "tree/rows", {"shape": "path", "node": "path", "key": "not json"}, 400, "not a tree node's key", id="a-key-that-is-no-key"),
        pytest.param("org", "tree", {"shape": "path", "node": "x" * 257}, 422, "at most 256 characters", id="an-over-long-name"),
    ],
)
def test_a_pick_the_table_cannot_answer_is_refused_by_name_before_anything_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, table: str, read: str, picks: dict[str, str], status: int, detail: str
) -> None:
    reads: list[list[int]] = []
    monkeypatch.setattr(derived_views, "_take", lambda ds, positions, projection: reads.append(positions) or ds.take(positions, columns=projection))

    refused = client.get(f"{BASE}/{table}/{read}", params=picks)

    assert (refused.status_code, detail in refused.text, reads) == (status, True, []), refused.text


def test_an_empty_table_is_drawn_empty(client: TestClient) -> None:
    graph = client.get(f"{BASE}/empty/graph", params={"shape": "edges", "source": "s", "target": "t"})
    atlas = client.get(f"{BASE}/empty/atlas", params={"x": "a", "y": "b"})
    tree = client.get(f"{BASE}/empty/tree", params={"shape": "path", "node": "s", "size": "a"})

    assert (graph.status_code, graph.json()["nodes"], atlas.status_code, _stream(atlas.content).num_rows) == (200, [], 200, 0)
    assert (tree.status_code, tree.json()["hierarchy"]["rows"]) == (200, 0)


def test_a_read_past_its_deadline_or_its_bytes_is_cut_and_says_so_and_work_past_it_stops(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derived_views, "FIRST_CHUNK_ROWS", 2)
    monkeypatch.setattr(derived_views, "READ_BYTES", 1)
    small = client.get(f"{BASE}/org/tree", params={"shape": "path", "node": "path"}).json()

    def slow(ds: lance.LanceDataset, positions: list[int], projection: dict[str, str]) -> pa.Table:
        time.sleep(0.8)
        return ds.take(positions, columns=projection)

    monkeypatch.setattr(derived_views, "_take", slow)
    monkeypatch.setattr(derived_views, "READ_BYTES", 1 << 30)
    monkeypatch.setattr(derived_views, "CHUNK_ROWS", 2)
    monkeypatch.setattr(derived_views, "DEADLINE_SECONDS", 2.4)
    cut = client.get(f"{BASE}/links/graph", params={"shape": "edges", "source": "src", "target": "dst"}).json()
    monkeypatch.setattr(derived_views, "DEADLINE_SECONDS", 0.0)
    stopped = client.get(f"{BASE}/org/graph", params={"shape": "edges", "source": "id", "target": "parent"})

    assert (cut["diagnostics"]["extent"]["read"], cut["diagnostics"]["extent"]["cut"]) == (4, "the read stopped after 1.6 s")
    assert (stopped.status_code, "did not answer within 0 s" in stopped.text) == (503, True)
    assert (small["diagnostics"]["extent"]["read"], small["diagnostics"]["extent"]["cut"]) == (2, "the read stopped at 0 MB of values")


def test_cold_builds_take_a_slot_one_answer_is_built_once_and_one_past_the_budget_is_not_kept(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[str] = []
    real = doors.build_atlas

    def counted(ds: lance.LanceDataset, columns: list[ColumnView], spec: AtlasSpec, *, within: Budget) -> bytes:
        builds.append("atlas")
        time.sleep(0.3)
        return real(ds, columns, spec, within=within)

    monkeypatch.setattr(doors, "build_atlas", counted)
    client = make_client(VIEWER_DERIVED_BUILDS=1)
    answers: list[int] = []
    callers = [
        threading.Thread(target=lambda: answers.append(client.get(f"{BASE}/points/atlas", params={"x": "east", "y": "north"}).status_code)) for _ in range(3)
    ]
    for caller in callers:
        caller.start()
    time.sleep(0.1)
    busy = client.get(f"{BASE}/points/atlas", params={"x": "north", "y": "east"})
    for caller in callers:
        caller.join()
    kept = client.get(f"{BASE}/points/atlas", params={"x": "east", "y": "north"}).status_code
    small = make_client(VIEWER_CACHE_BYTES=64)
    unkept = [small.get(f"{BASE}/points/atlas", params={"x": "east", "y": "north"}).status_code for _ in range(2)]

    assert (answers, busy.status_code, busy.headers.get("retry-after"), kept) == ([200, 200, 200], 503, "5", 200)
    assert (unkept, builds) == ([200, 200], ["atlas", "atlas", "atlas"])


def test_the_last_caller_to_leave_stops_the_build_at_its_next_check(tmp_path: Path, client: TestClient) -> None:
    """The caller's side: a client that went away is no longer waited for, and the last one to go stops the build. The
    build's side: a read stops at its next chunk once it is stopped."""

    class Left:
        async def is_disconnected(self) -> bool:
            return True

    async def scenario() -> bool:
        release = asyncio.Event()

        async def forever() -> bytes:
            await release.wait()
            return b""

        flight = doors._Flight(task=asyncio.ensure_future(forever()), cancelled=threading.Event())  # noqa: SLF001
        with pytest.raises(doors._Gone):  # noqa: SLF001
            await doors._wait(flight, cast(Request, Left()))  # noqa: SLF001
        stopped = flight.cancelled.is_set()
        release.set()
        await flight.task
        return stopped

    points = lance.dataset(namespace_client=namespace_client(tmp_path / "archive"), table_id=["field", "points"])

    assert asyncio.run(scenario()) is True
    with pytest.raises(derived_views.Abandoned):
        derived_views.read_spread(points, {"e": "`east`"}, 10, within=derived_views.Budget(time.monotonic() + 5, cancelled=_set()))


def _set() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


def test_a_new_version_of_the_table_is_drawn_afresh_and_the_profile_suggests_from_a_sample(client: TestClient, tmp_path: Path) -> None:
    picks = {"x": "east", "y": "north"}
    before = _stream(client.get(f"{BASE}/points/atlas", params=picks).content).num_rows
    lance.write_dataset(_points().slice(0, 2), namespace_client=namespace_client(tmp_path / "archive"), table_id=["field", "points"], mode="append")
    after = _stream(client.get(f"{BASE}/points/atlas", params=picks).content).num_rows
    profile = client.get(f"{BASE}/org/profile").json()
    times = {c["name"]: c["distinct"] for c in client.get(f"{BASE}/times/profile").json()["columns"]}
    by_name = {c["name"]: c for c in profile["columns"]}

    assert (before, after) == (10, 12)
    assert (by_name["path"]["separator"], round(by_name["path"]["separated"], 3), by_name["parent"]["refers_to"]) == ("/", 0.833, {"id": 0.8, "n": 0.8})
    assert (by_name["title"]["distinct"], by_name["parent"]["nulls"], by_name["path"]["examples"]) == (7, 1, ["a/b/c", "a/b/d", "a/e"])
    # A time or a duration feeds no role: the profile names it and reads none of it.
    assert times == {"t": None, "d": None, "s": 2}
