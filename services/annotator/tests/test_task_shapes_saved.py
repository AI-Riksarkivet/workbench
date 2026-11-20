"""What each labeling task's SAVED work actually looks like — proven on the real save door.

The e2e suites stub the save POST, so a field the client sends and the server silently drops is invisible to them: the
canvas shows the annotation, the payload asserts clean, and the draft holds it without the field. These tests drive the
canvas's save door over HTTP (`work_client`) and read the rows back as the canvas does, per task shape:

* a model PREDICTION (status/source/scores — the review queue's input),
* the composite OCR/HTR item (region classes + per-region transcription + a span + a page tag in ONE save — the
  layout-plus-content task every template composes toward),
* per-row attributes, through an insert and an edit.

Every field the frontend's ``InsertRow`` sends must be DECLARED on ``NewAnnotation``: pydantic ignores unknown fields by
default, so an undeclared field is not an error anywhere, it is a value that quietly never lands.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from annotator.text import MAX_JSON_DEPTH, MAX_MASK


UNIT = f"/api/annotations/{'a' * 16}/0/19"
QUERY = {"project": "p1", "task": "t1", "dataset": "scans~acme$proj1"}


def _saved(client: TestClient, base: int = 0, **body: Any) -> dict[str, dict[str, Any]]:
    """Save ``body`` on task t1's unit and answer the rows the canvas reads back, by id."""
    response = client.post(UNIT, params=QUERY, json={"base_version": base, **body})
    assert response.status_code == 200, response.text
    read = client.get(UNIT, params=QUERY)
    return {row["id"]: row for row in pa.ipc.open_stream(read.content).read_all().to_pylist()}


def test_a_prediction_saves_the_scores_the_review_queue_ranks_by(work_client: TestClient) -> None:
    prediction = {
        "id": "p1",
        "shape_type": "bbox",
        "x": 5,
        "y": 5,
        "width": 50,
        "height": 20,
        "label": "stamp",
        "status": "prediction",
        "source": "model:grounding-dino",
        "model_version": "2026.11",
        "confidence": 0.7,
        "uncertainty": 0.45,
    }

    r = _saved(work_client, inserts=[prediction])["p1"]

    assert (r["status"], r["source"], r["model_version"]) == ("prediction", "model:grounding-dino", "2026.11")
    assert r["confidence"] == pytest.approx(0.7) and r["uncertainty"] == pytest.approx(0.45)


_PROPOSAL = {"id": "p1", "shape_type": "bbox", "status": "prediction", "source": "model:detector", "model_version": "v1", "confidence": 0.7}


@pytest.mark.parametrize(
    ("save", "status", "said"),
    [
        pytest.param({"inserts": [{**_PROPOSAL, "model_version": None}]}, 422, "a model's row names the model's version and its score", id="no-version"),
        pytest.param({"inserts": [{**_PROPOSAL, "confidence": None}]}, 422, "a model's row names the model's version and its score", id="no-score"),
        pytest.param(
            {"inserts": [{**_PROPOSAL, "status": "predicted"}]},
            422,
            "Input should be 'accepted', 'reviewed', 'rejected' or 'prediction'",
            id="another-spelling",
        ),
        pytest.param({"inserts": [{**_PROPOSAL, "source": "human"}]}, 422, "a proposal names the model that made it", id="no-model"),
        pytest.param({"inserts": [{**_PROPOSAL, "status": "accepted", "source": "human"}]}, 422, "only a model's row names a model version", id="a-persons"),
        pytest.param({"edits": [{"id": "par", "status": "prediction"}]}, 400, "only a model's shape is a proposal", id="a-persons-made-a-proposal"),
    ],
)
def test_a_save_is_refused_when_a_proposal_does_not_say_what_made_it_or_a_persons_shape_claims_to_be_one(
    work_client: TestClient, save: dict[str, Any], status: int, said: str
) -> None:
    _saved(work_client, inserts=[{"id": "par", "shape_type": "bbox", "label": "paragraph"}])

    refused = work_client.post(UNIT, params=QUERY, json={"base_version": 1, **save})

    assert refused.status_code == status, refused.text
    assert said in refused.text


def test_the_composite_ocr_item_saves_as_one_coherent_revision(work_client: TestClient) -> None:
    """The layout-plus-content task: region classes, a transcription ON a region, a span INTO that transcription, and
    an item-level tag — one save, one revision, every relationship intact."""
    paragraph = {
        "id": "par",
        "shape_type": "polygon",
        "polygon": [10.0, 50.0, 210.0, 50.0, 210.0, 150.0, 10.0, 150.0],
        "label": "paragraph",
        "text": "för att samla allmogen mot unionskungen",
    }
    by_id = _saved(
        work_client,
        inserts=[
            {"id": "hdr", "shape_type": "bbox", "x": 10, "y": 5, "width": 200, "height": 30, "label": "header"},
            paragraph,
            {"id": "ent", "shape_type": "text", "label": "person", "parent_id": "par", "char_start": 27, "char_end": 39},
            {"id": "pg", "shape_type": "tag", "label": "damaged"},
        ],
    )

    assert sorted(by_id) == ["ent", "hdr", "par", "pg"]
    assert (by_id["hdr"]["label"], by_id["hdr"]["shape_type"]) == ("header", "bbox")
    # The transcription lives ON the region row — layout and content are one row, not two planes.
    assert (by_id["par"]["label"], by_id["par"]["text"]) == ("paragraph", "för att samla allmogen mot unionskungen")
    # The entity span anchors INTO the region's transcription by id + offsets; a row that is no span says -1.
    assert by_id["ent"]["parent_id"] == "par"
    assert by_id["par"]["text"][by_id["ent"]["char_start"] : by_id["ent"]["char_end"]] == "unionskungen"
    assert (by_id["hdr"]["char_start"], by_id["hdr"]["char_end"]) == (-1, -1)
    assert by_id["pg"]["shape_type"] == "tag"
    # Every row carries the SAME unit identity — the item stays one queryable thing.
    assert {(r["doc_id"], r["speech_id"], r["chunk_id"]) for r in by_id.values()} == {("a" * 16, 0, 19)}


def test_per_row_attributes_ride_metadata_through_an_insert_and_an_edit_stamped_by_the_server(work_client: TestClient) -> None:
    """The ontology's typed per-class attributes (`reading order`, `script`, …) are a JSON object of string values in
    `metadata`, the same flat map the submit validator parses, through the insert and the inspector's field edit alike.
    Who wrote the row is the verified caller, whatever the edit claims."""
    _saved(work_client, inserts=[{"id": "par", "shape_type": "polygon", "label": "paragraph", "metadata": '{"order": "2", "script": "cursive"}'}])

    after = _saved(work_client, 1, edits=[{"id": "par", "metadata": '{"order": 1, "script": "cursive"}', "reviewer": "mallory"}])["par"]

    assert json.loads(after["metadata"]) == {"order": "1", "script": "cursive"}
    assert (after["label"], after["reviewer"]) == ("paragraph", "anon")


@pytest.mark.parametrize(
    ("mask_chars", "status"),
    [
        pytest.param(1_324_798, 200, id="a-whole-page-brushed-mask"),
        pytest.param(MAX_MASK + 1, 422, id="past-the-bound"),
    ],
)
def test_a_mask_is_kept_up_to_its_bound_and_one_past_it_is_refused_naming_it(work_client: TestClient, mask_chars: int, status: int) -> None:
    """A mask is a PNG data URL of the region over its box; a 4096 x 4096 page with a brushed region measures 1.32 MB.
    One past `MAX_MASK` (half a draft's 8 MiB) is refused before its content is read, the field named."""
    mask = "data:image/png;base64," + "A" * (mask_chars - 22)

    r = work_client.post(UNIT, params=QUERY, json={"base_version": 0, "inserts": [{"id": "m1", "shape_type": "mask", "mask": mask}]})

    assert r.status_code == status, r.text[:300]
    if status == 422:
        assert [e["field"] for e in r.json()["errors"]] == ["body.inserts.0.mask"]


_BOX = {"shape_type": "bbox", "x": 1.0, "y": 1.0, "width": 2.0, "height": 2.0}


UNSTORABLE = (422, "the task's work cannot be kept: ")


def _nested(levels: int) -> str:
    """An attributes object whose one value nests ``levels - 1`` arrays: ``levels`` deep in all."""
    return '{"k":' + "[" * (levels - 1) + "]" * (levels - 1) + "}"


@pytest.mark.parametrize(
    ("change", "answer"),
    [
        pytest.param({"inserts": [{**_BOX, "id": "m1", "metadata": json.dumps({"k": "\x00"})}]}, UNSTORABLE, id="insert-a-nul-in-a-value"),
        pytest.param({"inserts": [{**_BOX, "id": "m1", "metadata": json.dumps({"k": "\ud800"})}]}, UNSTORABLE, id="insert-a-lone-surrogate-in-a-value"),
        pytest.param({"inserts": [{**_BOX, "id": "m1", "metadata": json.dumps({"\x00": "v"})}]}, UNSTORABLE, id="insert-a-nul-in-a-key"),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"k": "\x00"})}]}, UNSTORABLE, id="edit-a-nul-in-a-value"),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"k": "\ud800"})}]}, UNSTORABLE, id="edit-a-lone-surrogate-in-a-value"),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"k": "\\\x00"})}]}, UNSTORABLE, id="a-backslash-then-a-nul"),
        pytest.param(
            {"inserts": [{**_BOX, "id": "m1", "metadata": _nested(10_000)}]},
            (400, f"metadata nests deeper than {MAX_JSON_DEPTH} levels"),
            id="insert-json-ten-thousand-deep",
        ),
        pytest.param(
            {"edits": [{"id": "m0", "metadata": _nested(MAX_JSON_DEPTH + 1)}]},
            (400, f"metadata nests deeper than {MAX_JSON_DEPTH} levels"),
            id="one-level-past-the-depth-bound",
        ),
        pytest.param(
            {"edits": [{"id": "m0", "metadata": '{"k":' + "9" * 5000 + "}"}]},
            (400, "metadata is not valid JSON (Exceeds the limit (4300 digits)"),
            id="a-number-past-the-digit-limit",
        ),
        pytest.param(
            {"edits": [{"id": "m0", "metadata": _nested(MAX_JSON_DEPTH)}]},
            {"k": "[" * (MAX_JSON_DEPTH - 1) + "]" * (MAX_JSON_DEPTH - 1)},
            id="at-the-depth-bound",
        ),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"k": "\\u0000"})}]}, {"k": "\\u0000"}, id="a-backslash-then-the-text-u0000"),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"k": "\\\\u0000"})}]}, {"k": "\\\\u0000"}, id="two-backslashes-then-the-text"),
        pytest.param({"edits": [{"id": "m0", "metadata": json.dumps({"skrift": "kursiv å 😀"})}]}, {"skrift": "kursiv å 😀"}, id="non-ascii-text"),
    ],
)
def test_a_save_the_store_cannot_hold_is_refused_whole_and_any_other_is_kept(
    work_client: TestClient, change: dict[str, Any], answer: tuple[int, str] | dict[str, str]
) -> None:
    """Attributes ride the save as JSON text the door decodes. That text is decoded to a bounded depth (deeper is the
    door's 400, never the interpreter's recursion limit), and the store's write boundary holds the rest: a NUL or a lone
    surrogate anywhere in the document, a key included, is a 422 naming the task's work. A refused save leaves the draft as
    it was. A backslash followed by the text `u0000` is text, and is kept, as non-ASCII text is."""
    _saved(work_client, inserts=[{**_BOX, "id": "m0", "metadata": '{"k": "v"}'}])

    r = work_client.post(UNIT, params=QUERY, json={"base_version": 1, **change})

    rows = {row["id"]: row for row in pa.ipc.open_stream(work_client.get(UNIT, params=QUERY).content).read_all().to_pylist()}
    if isinstance(answer, tuple):
        status, detail = answer
        assert (r.status_code, r.json()["detail"].startswith(detail)) == (status, True), r.text[:300]
        assert (sorted(rows), json.loads(rows["m0"]["metadata"])) == (["m0"], {"k": "v"})
    else:
        assert r.status_code == 200, r.text[:300]
        assert json.loads(rows["m0"]["metadata"]) == answer
