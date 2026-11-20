"""A task's work in progress as the canvas reads and changes it: its draft's shapes as rows, and a save applied to them.

Pure: no store and no request. The doors (`annotator.annotations.wire`, `.save`, `.tags`) run these on the draft the
store's transaction read (`annotator.state.store.AnnotatorStore.change_draft`), so a change is judged and written on
rows that cannot move under it.

A save names rows by id, and every id it edits, moves or deletes must be one of the task's draft: one that is not is
refused with the ids it named (404), and nothing is written. A new shape takes an id of its own (409 when the draft
holds it). The server stamps who wrote each touched row and when; the caller's claim to either is never read.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Final, Literal

import pyarrow as pa

from annotator.annotations.schema import EMPTY_SCHEMA, NewAnnotation, SaveAnnotations
from annotator.annotations.unit import Unit
from annotator.projects.models import MODEL_SOURCE, PROPOSED, Draft, Link, Shape
from annotator.text import MAX_METADATA, embedded_json
from service_kit.exceptions import ConflictError, NotFoundError, ValidationError


#: How many ids a refusal names: enough to find them, bounded so a refusal is never as large as the save.
NAMED_IDS: Final = 20


def attributes_of(metadata: str) -> dict[str, str]:
    """The ontology's attribute map a row's ``metadata`` JSON carries: an object, its values as strings (a number or a
    boolean spelled as JSON is the same value), nested values as their JSON text.

    Raises:
        ValidationError: ``metadata`` is not a JSON object, or nests deeper than `annotator.text.MAX_JSON_DEPTH`.
    """
    try:
        parsed = embedded_json(metadata, max_chars=MAX_METADATA) if metadata else {}
    except ValueError as exc:
        raise ValidationError(f"metadata {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("metadata is a JSON object of attributes")
    return {str(k): v if isinstance(v, str) else ("" if v is None else json.dumps(v, separators=(",", ":"))) for k, v in parsed.items()}


def _metadata(attributes: dict[str, str]) -> str:
    return json.dumps(attributes, separators=(",", ":"), sort_keys=True)


#: What a person does with every proposal of an item at once.
type SettleAction = Literal["keep", "remove"]


def _new_shape(row: NewAnnotation, *, author: str, now: datetime) -> Shape:
    return Shape(
        shape_id=row.id,
        shape_type=row.shape_type,
        x=row.x,
        y=row.y,
        width=row.width,
        height=row.height,
        rotation=row.rotation,
        polygon=list(row.polygon),
        t_start=row.t_start,
        t_end=row.t_end,
        parent_id=row.parent_id or None,
        char_start=row.char_start if row.char_start >= 0 else None,
        char_end=row.char_end if row.char_end >= 0 else None,
        mask=row.mask or None,
        label=row.label,
        text=row.text,
        attributes=attributes_of(row.metadata),
        group=row.group or None,
        source=row.source,
        model_version=row.model_version,
        confidence=row.confidence,
        uncertainty=row.uncertainty,
        status=row.status,
        reviewer=author,
        created_at=now,
        updated_at=now,
    )


def _refuse_unheld(ids: set[str], held: dict[str, Shape]) -> None:
    if unknown := sorted(ids - held.keys()):
        raise NotFoundError(f"annotation(s) {unknown[:NAMED_IDS]} are not this task's rows of its unit")


def apply_save(draft: Draft | None, body: SaveAnnotations, *, author: str, now: datetime) -> tuple[list[Shape], list[Link]]:
    """The draft's shapes and links once ``body`` is applied to them.

    Raises:
        NotFoundError: The save edits, moves or deletes an id the draft does not hold.
        ConflictError: A new shape names an id the draft holds.
        ValidationError: A metadata edit is not a JSON object, or a link names a shape the draft will not hold.
    """
    shapes = {s.shape_id: s.model_copy() for s in (draft.shapes if draft else [])}
    links = list(draft.links) if draft else []
    if held := sorted({i.id for i in body.inserts} & shapes.keys()):
        raise ConflictError(f"annotation(s) {held[:NAMED_IDS]} already exist: a new shape takes an id of its own")
    # New shapes first: the canvas sends a shape drawn and then moved, relabelled or deleted before the save as its
    # insert and those changes together, by its new id.
    for row in body.inserts:
        shapes[row.id] = _new_shape(row, author=author, now=now)
    named = {e.id for e in body.edits} | {g.id for g in body.geometry} | {t.id for t in body.temporal} | {s.id for s in body.spans} | set(body.deletes)
    _refuse_unheld(named, shapes)

    touched: set[str] = set()
    for edit in body.edits:
        shape = shapes[edit.id]
        if edit.status == PROPOSED and not (shape.source or "").startswith(MODEL_SOURCE):
            raise ValidationError(f"annotation {edit.id} is a person's: only a model's shape is a proposal")
        for field in ("label", "status", "text", "group"):
            value = getattr(edit, field)
            if value is not None:
                setattr(shape, field, value)
        if edit.metadata is not None:
            shape.attributes = attributes_of(edit.metadata)
        touched.add(edit.id)
    for move in body.geometry:
        shape = shapes[move.id]
        shape.x, shape.y, shape.width, shape.height, shape.polygon = move.x, move.y, move.width, move.height, list(move.polygon)
        touched.add(move.id)
    for stretch in body.temporal:
        shapes[stretch.id].t_start, shapes[stretch.id].t_end = stretch.t_start, stretch.t_end
        touched.add(stretch.id)
    for span in body.spans:
        shapes[span.id].char_start, shapes[span.id].char_end = span.char_start, span.char_end
        touched.add(span.id)
    for shape_id in touched:
        shapes[shape_id].reviewer, shapes[shape_id].updated_at = author, now

    gone = set(body.deletes)
    for shape_id in gone:
        del shapes[shape_id]

    if body.links is not None:
        links = list({(link.name, link.from_shape, link.to_shape): link.stored() for link in body.links}.values())
        if dangling := sorted({end for link in links for end in (link.from_shape, link.to_shape)} - shapes.keys()):
            raise ValidationError(f"link(s) name shape(s) {dangling[:NAMED_IDS]} this task's work does not hold")
    else:
        # A deleted shape's links go with it.
        links = [link for link in links if link.from_shape not in gone and link.to_shape not in gone]
    return list(shapes.values()), links


def settle_proposals(draft: Draft | None, action: SettleAction, *, author: str, now: datetime) -> tuple[list[Shape], list[Link], int]:
    """The draft's shapes and links once every proposal in it is kept or removed, and how many there were.

    Kept, a proposal becomes a label the person stands behind (`accepted`, with them as its reviewer) and still names
    the model that made it, its version and its score. Removed, it goes with its links.
    """
    shapes = [s.model_copy() for s in (draft.shapes if draft else [])]
    links = list(draft.links) if draft else []
    proposed = {s.shape_id for s in shapes if s.status == PROPOSED}
    if action == "remove":
        return (
            [s for s in shapes if s.shape_id not in proposed],
            [k for k in links if k.from_shape not in proposed and k.to_shape not in proposed],
            len(proposed),
        )
    for shape in shapes:
        if shape.shape_id in proposed:
            shape.status, shape.reviewer, shape.updated_at = "accepted", author, now
    return shapes, links, len(proposed)


def _id_type(value: str | int) -> pa.DataType:
    return pa.string() if isinstance(value, str) else pa.int64()


def wire_table(unit: Unit, task_id: str, draft: Draft | None) -> pa.Table:
    """The draft's shapes as the rows the canvas reads: the unit's identity columns, then `EMPTY_SCHEMA`'s. A row's
    ``links`` are the links leaving it."""
    schema = pa.schema([*(pa.field(name, _id_type(value)) for name, value in unit.values.items()), *EMPTY_SCHEMA])
    shapes = draft.shapes if draft else []
    outgoing: dict[str, list[dict[str, str]]] = {}
    for link in draft.links if draft else []:
        outgoing.setdefault(link.from_shape, []).append({"name": link.name, "to_shape": link.to_shape})
    rows = [
        {
            **unit.values,
            "id": s.shape_id,
            "task_id": task_id,
            "shape_type": s.shape_type,
            "x": s.x,
            "y": s.y,
            "width": s.width,
            "height": s.height,
            "rotation": s.rotation,
            "polygon": s.polygon,
            "t_start": s.t_start,
            "t_end": s.t_end,
            "text": s.text or "",
            "parent_id": s.parent_id or "",
            "char_start": -1 if s.char_start is None else s.char_start,
            "char_end": -1 if s.char_end is None else s.char_end,
            "label": s.label or "",
            "status": s.status,
            "source": s.source or "",
            "reviewer": s.reviewer,
            "confidence": s.confidence,
            "uncertainty": s.uncertainty,
            "model_version": s.model_version,
            "group": s.group or "",
            "difficult": s.difficult,
            "links": json.dumps(outgoing.get(s.shape_id, []), separators=(",", ":")),
            "mask": s.mask or "",
            "metadata": _metadata(s.attributes),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in shapes
    ]
    return pa.Table.from_pylist(rows, schema=schema)


__all__ = ["apply_save", "attributes_of", "wire_table"]
