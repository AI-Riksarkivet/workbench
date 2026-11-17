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

from annotator.api.v1.endpoints.assist import _CANONICAL_SHAPE, AssistShape, _task_ontology, _within_contract
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task
from annotator.projects.ontology import LabelOntology
from annotator.state.store import AnnotatorStore


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
        "confidence": 0.7,
        "uncertainty": 0.45,
    }

    r = _saved(work_client, inserts=[prediction])["p1"]

    assert r["status"] == "prediction" and r["source"] == "model:grounding-dino"
    assert r["confidence"] == pytest.approx(0.7) and r["uncertainty"] == pytest.approx(0.45)


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


@pytest.mark.asyncio
async def test_assist_predictions_are_filtered_by_the_composite_ontology(state_settings: AnnotatorSettings) -> None:
    """The ML-backend interplay for a CUSTOM task: the server checks every producer shape against
    the task's own taxonomy — the union of per-class tools, read from the task the store holds — before it reaches a
    human. A mask prediction against a polygon/bbox/text layout task is dropped AND reported, not queued."""
    ontology = LabelOntology.model_validate(
        {
            "kind": "ocr-layout",
            "classes": [
                {"name": "header", "tools": ["bbox"]},
                {"name": "paragraph", "tools": ["polygon", "bbox"]},
                {"name": "marginalia", "tools": ["polygon"]},
                {"name": "transcription", "tools": ["text"]},
            ],
        }
    )
    store = await AnnotatorStore.open(state_settings)
    try:
        await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="layout", state=ProjectState.LABELING))
        await store.send_tasks(
            "p1", [Task(task_id="t1", project_id="p1", source=ItemSource(kind="chunks", keys=["d1"]), media=MediaRef(kind="image"), ontology=ontology)]
        )
        contract = await _task_ontology(store, "p1", "t1")
    finally:
        await store.close()

    def shape(t: str) -> AssistShape:
        return AssistShape(shape_type=t, x=0, y=0, width=10, height=10)

    # The route normalizes producer dialects BEFORE the filter (assist.py's canonicalization
    # loop) — mirrored here because `_within_contract` sees only canonical types in production.
    shapes = [shape("polygon"), shape("rectangle"), shape("mask")]
    for s in shapes:
        s.shape_type = _CANONICAL_SHAPE.get(s.shape_type, s.shape_type)

    kept, dropped = _within_contract(shapes, contract)

    # `rectangle` (a producer dialect) normalized to `bbox` and accepted; `mask` refused.
    assert [s.shape_type for s in kept] == ["polygon", "bbox"]
    assert len(dropped) == 1 and "mask" in dropped[0]
