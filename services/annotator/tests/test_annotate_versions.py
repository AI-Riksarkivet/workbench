"""A task's work in progress is its draft: the canvas's save lands there, its reads come from there, and each save is kept
in a bounded history the canvas reads back and travels to.

Driven through the real routes (`work_client`: the annotations and task doors over the state store on a real
PostgreSQL), on a dataset of a source outside rask that git marks annotatable, with no lakehouse declared: labeling
needs none.
"""

from __future__ import annotations

import io
import json
from typing import Any

import psycopg
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from annotator.annotations.schema import MAX_SAVE_ROWS
from annotator.state.store import MAX_DRAFT_BYTES, MAX_DRAFT_HISTORY, MAX_DRAFT_LINKS, MAX_DRAFT_SHAPES, MAX_HISTORY_BYTES
from tests.postgres import StateDatabase


DATASET = "scans~acme$proj1"
UNIT = f"/api/annotations/{'a' * 16}/0/19"
QUERY = {"project": "p1", "task": "t1", "dataset": DATASET}


def _save(client: TestClient, base: int, **body: Any) -> Any:
    response = client.post(UNIT, params=QUERY, json={"base_version": base, **body})
    assert response.status_code == 200, response.text
    return response.json()


def _rows(client: TestClient, **query: Any) -> tuple[list[dict[str, Any]], str]:
    response = client.get(UNIT, params={**QUERY, **query})
    assert response.status_code == 200, response.text
    return pa.ipc.open_stream(response.content).read_all().to_pylist(), response.headers["X-Annotations-Version"]


def test_a_save_changes_the_tasks_draft_and_the_canvas_reads_its_rows_and_links_from_it(work_client: TestClient) -> None:
    """Drawn shapes (one relabelled before the save, which sends its insert and its edit together), an edit, a move and
    a relation drawn on its own each land in the draft the submit and the publish read, and the canvas's read serves the
    same rows, each with the links leaving it, at the draft's revision."""
    _save(
        work_client,
        0,
        inserts=[
            {"id": "q", "shape_type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4, "label": "question"},
            {"id": "a", "shape_type": "bbox", "label": "", "metadata": '{"order":"1"}'},
        ],
        edits=[{"id": "a", "label": "answer"}],
    )
    _save(work_client, 1, edits=[{"id": "a", "metadata": '{"order":"2"}'}], geometry=[{"id": "q", "x": 5, "y": 6, "width": 7, "height": 8}])
    _save(work_client, 2, links=[{"name": "answered_by", "from_shape": "q", "to_shape": "a"}])

    draft = work_client.get("/tasks/t1/draft").json()
    rows, version = _rows(work_client)

    assert draft["revision"] == 3
    assert draft["links"] == [{"name": "answered_by", "from_shape": "q", "to_shape": "a"}]
    assert [(s["shape_id"], s["label"], s["x"], s["attributes"], s["reviewer"]) for s in draft["shapes"]] == [
        ("q", "question", 5.0, {}, "anon"),
        ("a", "answer", 0.0, {"order": "2"}, "anon"),
    ]
    assert version == "3"
    assert [(r["doc_id"], r["chunk_id"], r["id"], r["task_id"], r["links"], r["metadata"]) for r in rows] == [
        ("a" * 16, 19, "q", "t1", '[{"name":"answered_by","to_shape":"a"}]', "{}"),
        ("a" * 16, 19, "a", "t1", "[]", '{"order":"2"}'),
    ]


def test_deleting_a_shape_takes_its_links_and_a_link_to_a_shape_the_work_lacks_is_refused(work_client: TestClient) -> None:
    _save(work_client, 0, inserts=[{"id": "q", "shape_type": "bbox"}, {"id": "a", "shape_type": "bbox"}])
    _save(work_client, 1, links=[{"name": "answered_by", "from_shape": "q", "to_shape": "a"}])

    _save(work_client, 2, deletes=["a"])
    dangling = work_client.post(UNIT, params=QUERY, json={"base_version": 3, "links": [{"name": "answered_by", "from_shape": "q", "to_shape": "a"}]})

    assert work_client.get("/tasks/t1/draft").json()["links"] == []
    assert (dangling.status_code, dangling.json()["detail"]) == (400, "link(s) name shape(s) ['a'] this task's work does not hold")


def test_the_history_lists_each_save_newest_first_and_a_past_save_reads_back_as_it_was(work_client: TestClient) -> None:
    """A past save reads back as it was, whatever the saves after it did to its rows and links: an edit, a delete, a
    relation drawn. An empty save changes nothing, so it writes nothing and takes no place in the history."""
    _save(work_client, 0, inserts=[{"id": "a1", "shape_type": "bbox", "label": "first"}, {"id": "a2", "shape_type": "bbox"}])
    _save(work_client, 1, edits=[{"id": "a1", "label": "second"}], links=[{"name": "next", "from_shape": "a1", "to_shape": "a2"}])
    _save(work_client, 2, deletes=["a1"], inserts=[{"id": "a3", "shape_type": "bbox"}])
    empty = _save(work_client, 3)

    history = work_client.get(f"{UNIT}/versions", params=QUERY).json()
    first, at_first = _rows(work_client, version=1)
    second, at_second = _rows(work_client, version=2)

    assert empty == {"saved": 0, "version": 3}
    assert [(entry["version"], entry["count"], entry["author"]) for entry in history] == [(3, 2, "anon"), (2, 2, "anon"), (1, 2, "anon")]
    assert ({row["id"]: (row["label"], row["links"]) for row in first}, at_first) == ({"a1": ("first", "[]"), "a2": ("", "[]")}, "1")
    assert {row["id"]: (row["label"], row["links"]) for row in second} == {"a1": ("second", '[{"name":"next","to_shape":"a2"}]'), "a2": ("", "[]")}
    assert at_second == "2"


def test_the_history_keeps_only_a_tasks_last_saves(work_client: TestClient) -> None:
    """The history is bounded in saves: past `MAX_DRAFT_HISTORY` the oldest goes, in the transaction of the save that
    pushed it out, and a read of it is the 404 of a version the history does not keep."""
    for base in range(MAX_DRAFT_HISTORY + 2):
        _save(work_client, base, inserts=[{"id": f"s{base}", "shape_type": "tag", "label": "x"}])

    kept = work_client.get(f"{UNIT}/versions", params={**QUERY, "limit": MAX_DRAFT_HISTORY}).json()
    gone = work_client.get(UNIT, params={**QUERY, "version": 2})

    assert [entry["version"] for entry in kept] == list(range(MAX_DRAFT_HISTORY + 2, 2, -1))
    assert gone.status_code == 404, gone.text
    assert gone.json()["detail"] == "annotations version 2 not found: this item's history does not keep it"


def test_the_history_keeps_the_last_saves_its_byte_bound_holds(work_client: TestClient) -> None:
    """The history keeps each save's change, not the whole draft, and no more of them than `MAX_HISTORY_BYTES` holds: a
    task's work costs at most a capped draft and that much history. Saves that each replace a large part of the work push
    the oldest out, and a read of one pushed out is the 404 of a version the history does not keep."""
    text = "x" * (MAX_DRAFT_BYTES // 400)
    rounds = 2 * MAX_HISTORY_BYTES // (MAX_DRAFT_BYTES // 2) + 1
    for base in range(rounds):
        _save(
            work_client,
            base,
            inserts=[{"id": f"r{base}-{i}", "shape_type": "tag", "text": text} for i in range(150)],
            deletes=[f"r{base - 1}-{i}" for i in range(150)] if base else [],
        )

    kept = [entry["version"] for entry in work_client.get(f"{UNIT}/versions", params={**QUERY, "limit": MAX_DRAFT_HISTORY}).json()]
    oldest = work_client.get(UNIT, params={**QUERY, "version": kept[-1]})
    gone = work_client.get(UNIT, params={**QUERY, "version": kept[-1] - 1})

    assert kept == list(range(rounds, kept[-1] - 1, -1)) and 1 < kept[-1] < rounds
    assert {row["id"] for row in pa.ipc.open_stream(oldest.content).read_all().to_pylist()} == {f"r{kept[-1] - 1}-{i}" for i in range(150)}
    assert gone.status_code == 404, gone.text


def _ipc(rows: list[dict[str, Any]]) -> bytes:
    sink = io.BytesIO()
    table = pa.Table.from_pylist(rows)
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.parametrize(
    ("door", "status", "limit"),
    [
        pytest.param("save", 413, f"{MAX_DRAFT_SHAPES} shapes", id="shapes"),
        pytest.param("import", 413, f"{MAX_DRAFT_LINKS} links", id="links"),
        pytest.param("big", 413, f"{MAX_DRAFT_BYTES} bytes", id="bytes"),
        pytest.param("tags", 422, f"names at most {MAX_SAVE_ROWS}", id="a-tag-batch-past-one-saves-rows"),
    ],
)
def test_work_that_would_pass_its_bound_is_refused_and_nothing_is_written(work_client: TestClient, door: str, status: int, limit: str) -> None:
    """A task's work is held to its caps whichever door changes it: the save, the tag batch and the import all go through
    the one change of the draft that checks them before it writes, and a tag batch names no more rows than a save."""
    _save(work_client, 0, inserts=[{"id": "kept", "shape_type": "tag", "label": "x"}])
    if door == "save":
        # The work holds one shape, then fills to exactly its cap.
        for base, count in ((1, MAX_DRAFT_SHAPES // 2), (2, MAX_DRAFT_SHAPES - MAX_DRAFT_SHAPES // 2 - 1)):
            _save(work_client, base, inserts=[{"id": f"s{base}-{i}", "shape_type": "tag"} for i in range(count)])
        at = 3
        refused = work_client.post(UNIT, params=QUERY, json={"base_version": at, "inserts": [{"id": "one-more", "shape_type": "tag"}]})
    elif door == "import":
        at = 1
        rows = [{"id": "q", "shape_type": "bbox", "links": json.dumps([{"name": f"n{i}", "to_shape": "kept"} for i in range(MAX_DRAFT_LINKS + 1)])}]
        refused = work_client.post("/tasks/t1/import", content=_ipc(rows), headers={"content-type": "application/vnd.apache.arrow.stream"})
    elif door == "tags":
        at = 1
        labels = [f"l{i}" for i in range(MAX_SAVE_ROWS // 2 + 1)]
        refused = work_client.post("/api/annotations/tags", params=QUERY, json={"adds": [{"doc_id": "a" * 16, "keys": [0, 19], "labels": labels}] * 2})
    else:
        at = 1
        big = "x" * (MAX_DRAFT_BYTES // 100)
        refused = work_client.post(
            UNIT, params=QUERY, json={"base_version": at, "inserts": [{"id": f"b{i}", "shape_type": "tag", "text": big} for i in range(101)]}
        )

    assert refused.status_code == status, refused.text
    assert limit in json.dumps(refused.json())
    assert work_client.get("/tasks/t1/draft").json()["revision"] == at


def test_a_save_into_a_project_frozen_with_a_publish_is_refused_and_writes_nothing(work_client: TestClient, state_database: StateDatabase) -> None:
    """The publish reads the drafts to build its rows: once it runs, no save may change them under it."""
    _save(work_client, 0, inserts=[{"id": "kept", "shape_type": "bbox"}])
    with psycopg.connect(host=state_database.host, port=state_database.port, user=state_database.user, dbname=state_database.database) as conn:
        conn.execute("UPDATE projects SET state = 'publishing', document = jsonb_set(document, '{state}', '\"publishing\"') WHERE project_id = 'p1'")

    refused = work_client.post(UNIT, params=QUERY, json={"base_version": 1, "inserts": [{"id": "late", "shape_type": "bbox"}]})

    assert refused.status_code == 409, refused.text
    assert "frozen with the published artifact" in refused.json()["detail"]
    assert [s["shape_id"] for s in work_client.get("/tasks/t1/draft").json()["shapes"]] == ["kept"]


def test_a_task_with_no_work_yet_reads_as_no_rows_at_version_zero_and_no_history(work_client: TestClient) -> None:
    rows, version = _rows(work_client)

    assert (rows, version) == ([], "0")
    assert work_client.get(f"{UNIT}/versions", params=QUERY).json() == []


def _work(client: TestClient) -> tuple[Any, Any, int]:
    draft = client.get("/tasks/t1/draft").json()
    return draft["shapes"], draft["links"], draft["revision"]


def test_a_save_reverted_on_the_server_brings_back_what_it_replaced_whole_and_reverting_the_revert_redoes_it(work_client: TestClient) -> None:
    """A bulk label "set dog" replaces a model's prediction tag that carries text, an attribute, its source, status and
    score, and a box's link to it. Reverting that save puts the work back exactly as it was before it, shapes and links,
    but its revision; reverting the revert puts back the label."""
    _save(
        work_client,
        0,
        inserts=[
            {"id": "b", "shape_type": "bbox", "x": 1, "y": 1, "width": 2, "height": 2, "label": "stamp"},
            {
                "id": "t1",
                "shape_type": "tag",
                "label": "cat",
                "text": "a cat on the stamp",
                "metadata": '{"pose":"sitting"}',
                "source": "model:vlm",
                "model_version": "vlm.1",
                "status": "prediction",
                "confidence": 0.91,
            },
        ],
        links=[{"name": "shows", "from_shape": "b", "to_shape": "t1"}],
    )
    before = _work(work_client)
    _save(work_client, 1, deletes=["t1"], inserts=[{"id": "t2", "shape_type": "tag", "label": "dog"}])
    labelled = _work(work_client)

    undone = work_client.post(f"{UNIT}/revert", params=QUERY, json={"revision": 2})
    after_undo = _work(work_client)
    redone = work_client.post(f"{UNIT}/revert", params=QUERY, json={"revision": 3})

    assert (undone.status_code, undone.json()) == (200, {"saved": 2, "version": 3})
    assert after_undo == (before[0], before[1], 3)
    assert (redone.status_code, _work(work_client)) == (200, (labelled[0], labelled[1], 4))


@pytest.mark.parametrize(
    ("revision", "detail"),
    [
        pytest.param(1, "annotations changed on the server (loaded v1, now v2)", id="work-changed-since"),
        pytest.param(5, "annotations changed on the server (loaded v5, now v2)", id="a-revision-it-never-reached"),
    ],
)
def test_a_revert_of_a_save_the_work_has_moved_past_is_refused_and_writes_nothing(work_client: TestClient, revision: int, detail: str) -> None:
    _save(work_client, 0, inserts=[{"id": "a", "shape_type": "tag", "label": "cat"}])
    _save(work_client, 1, edits=[{"id": "a", "label": "dog"}])

    refused = work_client.post(f"{UNIT}/revert", params=QUERY, json={"revision": revision})

    assert (refused.status_code, refused.json()["detail"]) == (409, detail)
    assert _work(work_client)[2] == 2


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"revision": "1"}, id="a-revision-written-as-text"),
        pytest.param({"revision": 1, "base_version": 1}, id="a-key-the-door-does-not-read"),
    ],
)
def test_a_revert_request_the_door_would_have_to_guess_at_is_refused_and_writes_nothing(work_client: TestClient, body: dict[str, Any]) -> None:
    _save(work_client, 0, inserts=[{"id": "a", "shape_type": "tag", "label": "cat"}])

    refused = work_client.post(f"{UNIT}/revert", params=QUERY, json=body)

    assert refused.status_code == 422, refused.text
    assert _work(work_client)[2] == 1
