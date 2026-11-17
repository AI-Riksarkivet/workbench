"""The annotations CONTRACT — the canvas's request/response models and the columns of the rows it reads.

One place for the shapes every annotations module shares. ``EMPTY_SCHEMA`` is the columns of the Arrow stream the
canvas reads a unit's work in (`annotator.annotations.wire`), built from the task's draft
(`annotator.annotations.work.wire_table`), with the unit's identity columns before them.
"""

from __future__ import annotations

from typing import Annotated, Self

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from annotator.projects.models import Link, ShapeType


#: An annotation's id: the canvas mints ids of a few dozen characters, and a chunk tag's id names its document, its keys
#: and its percent-encoded label (`annotator.annotations.tags.tag_id`), a few hundred at most.
MAX_ID_LENGTH = 1024

#: A track's group: the canvas mints a dozen characters (`trk-` and a base-36 number).
MAX_GROUP_LENGTH = 128

#: The rows one save may name, every list counted: an autosave carries what changed since the last one, a few dozen
#: rows, and a bulk load is an import, which has its own door. Bounds the change one request applies to a draft.
MAX_SAVE_ROWS = 5000

#: The links one save may state: the whole set of the item's relations, which a person draws one by one.
MAX_SAVE_LINKS = 5000

RowId = Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
#: A tag's label: a class name, as long as a sender's prediction label may be.
Label = Annotated[str, Field(min_length=1, max_length=128)]
Group = Annotated[str, Field(max_length=MAX_GROUP_LENGTH)]
#: A moment in the item's media, in seconds from its start.
Seconds = Annotated[float, Field(ge=0.0)]
#: The largest offset the table's `char_start`/`char_end` (int32) holds.
INT32_MAX = 2**31 - 1
#: A character of a row's text, in code points from its start: what the canvas sends and Python slices by.
Offset = Annotated[int, Field(ge=0, le=INT32_MAX)]

#: Every number a saved row carries is finite: a NaN or an infinity read back breaks every reader of the table.
FINITE = ConfigDict(allow_inf_nan=False)


def _ends_after_it_starts(t_start: float, t_end: float) -> None:
    if t_end < t_start:
        raise ValueError(f"t_end {t_end} is before t_start {t_start}")


def _a_range(char_start: int, char_end: int) -> None:
    if char_end < char_start:
        raise ValueError(f"char_end {char_end} is before char_start {char_start}")


#: The fields a reviewer may edit — the local-first review overlay flushed on save.
#: Only these are patched; geometry + provenance columns are carried forward from the
#: current row, so a partial edit never wipes a shape. ``metadata`` is the per-row
#: ATTRIBUTES carrier: a JSON object of ``{attribute name: value-as-string}``, the same
#: flat map the ontology's typed attributes are validated from at submit.
EDITABLE_FIELDS = ("label", "status", "text", "group", "reviewer", "metadata")


class AnnotationEdit(BaseModel):
    """One reviewed annotation: its id + only the fields that changed."""

    id: RowId
    label: str | None = None
    status: str | None = None
    text: str | None = None
    group: Group | None = None
    reviewer: str | None = None
    metadata: str | None = None


class NewAnnotation(BaseModel):
    """A newly drawn shape. Geometry + attributes; the chunk identity columns are
    stamped server-side from the route keys, so the client sends only shape data.
    A human-drawn shape is ``accepted`` by construction, ``source=human``. These
    defaults are ALSO the row defaults for server-minted rows (chunk tags), so the
    contract lives in one model."""

    model_config = FINITE

    id: RowId
    #: The canonical vocabulary only (the canvas maps its tools' names into it, `@workbench/labeling/shape-types`): a
    #: draft holds no shape the submit, the ontology and the publish do not speak.
    shape_type: ShapeType
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    rotation: float = 0.0
    polygon: list[float] = Field(default_factory=list)
    t_start: Seconds = 0.0
    t_end: Seconds = 0.0
    text: str = ""
    label: str = ""
    status: str = "accepted"
    source: str = "human"
    group: Group = ""
    mask: str = ""
    #: The TEXTUAL facet — a span INTO another row's ``text`` (token classification, the value half
    #: of KIE). Sentinels match the client's ``makeInsertRow``: empty/-1 on every non-span row, so a
    #: default never fakes a zero-length span at offset 0. These MUST be declared here even though
    #: the table schema already carries the columns: pydantic ignores unknown payload fields, so an
    #: undeclared field is not a 422 — it is a column that silently lands null. Text spans shipped
    #: exactly that way: rendered from the client overlay, saved, and read back pointing at nothing.
    parent_id: str = ""
    char_start: Annotated[int, Field(ge=-1, le=INT32_MAX)] = -1
    char_end: Annotated[int, Field(ge=-1, le=INT32_MAX)] = -1
    #: Per-row ATTRIBUTES — the ontology's typed per-class fields (`reading order`, `script`,
    #: `damaged`…), as a JSON object of string values (the submit validator parses int/bool/enum
    #: from strings). ``{}`` rather than ``""``: the column is `pa.json_()` and an empty string is
    #: not valid JSON.
    metadata: str = "{}"
    #: The active-learning scores a PREDICTION row carries (the review queue ranks by them). None —
    #: the human-drawn default — lands as null: a person's shape has no model score, and inventing
    #: one (1.0 "confidence") would let human rows shuffle into the model-ranked half of the queue.
    confidence: float | None = None
    uncertainty: float | None = None

    @model_validator(mode="after")
    def _a_stretch_ends_after_it_starts(self) -> Self:
        _ends_after_it_starts(self.t_start, self.t_end)
        # -1 on both ends is a row that is no span; a span's range is two offsets, the end not before the start.
        if (self.char_start, self.char_end) != (-1, -1):
            if self.char_start < 0 or self.char_end < 0:
                raise ValueError(f"a span's range is two offsets, not ({self.char_start}, {self.char_end})")
            _a_range(self.char_start, self.char_end)
        return self


class GeometryEdit(BaseModel):
    """A moved/resized EXISTING shape — spatial geometry only, keyed by id (canvas drag)."""

    model_config = FINITE

    id: RowId
    x: float
    y: float
    width: float
    height: float
    polygon: list[float] = Field(default_factory=list)


class TemporalEdit(BaseModel):
    """A resized audio/video SEGMENT — its times only, keyed by id (waveform region drag)."""

    model_config = FINITE

    id: RowId
    t_start: Seconds
    t_end: Seconds

    @model_validator(mode="after")
    def _a_stretch_ends_after_it_starts(self) -> Self:
        _ends_after_it_starts(self.t_start, self.t_end)
        return self


class SpanEdit(BaseModel):
    """A re-anchored TEXT SPAN — its offsets only, keyed by id.

    Exists for the transcription-edit remap: correcting a parent's text shifts the text under
    every span anchored to it, and the client recomputes each span's `[start, end)` to follow.
    Offsets are deliberately NOT in `EDITABLE_FIELDS` — a free-form field edit of a char offset
    is a corruption vector; this channel is the one legal writer, mirroring `TemporalEdit`."""

    id: RowId
    char_start: Offset
    char_end: Offset

    @model_validator(mode="after")
    def _a_range_ends_after_it_starts(self) -> Self:
        _a_range(self.char_start, self.char_end)
        return self


class SaveAnnotations(BaseModel):
    """The change a Save makes to one unit's work, applied to the task's draft in one transaction: field edits, newly
    drawn shapes, moved geometry, resized times, re-anchored spans, deleted ids and, when the relations changed, the
    whole set of the item's links.

    ``base_version`` is the draft's revision the client loaded (0 before the first save): the save 409s when the
    task's work moved underneath it (another tab, a reviewer).
    """

    edits: list[AnnotationEdit] = Field(default_factory=list)
    inserts: list[NewAnnotation] = Field(default_factory=list)
    geometry: list[GeometryEdit] = Field(default_factory=list)
    temporal: list[TemporalEdit] = Field(default_factory=list)
    spans: list[SpanEdit] = Field(default_factory=list)
    deletes: list[RowId] = Field(default_factory=list)
    #: The item's links after this save, all of them, or None to leave them as they are (a deleted shape's links go
    #: with it either way).
    links: list[Link] | None = Field(default=None, max_length=MAX_SAVE_LINKS)
    base_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        rows = len(self.edits) + len(self.inserts) + len(self.geometry) + len(self.temporal) + len(self.spans) + len(self.deletes)
        if rows > MAX_SAVE_ROWS:
            raise ValueError(f"a save names {rows} rows, and one names at most {MAX_SAVE_ROWS}: save more often, or import")
        ids = [i.id for i in self.inserts]
        if len(set(ids)) != len(ids):
            raise ValueError("two new shapes in one save carry the same id")
        return self


class SaveResult(BaseModel):
    """One save. ``saved`` counts the rows it touched (edits, inserts and deletes); ``version`` is the draft's revision
    it wrote."""

    saved: int
    version: int


class TagWrite(BaseModel):
    """One unit + the tag labels to set on it. ``keys`` are the NON-doc identity fields, positional — they pair with
    ``descriptor.identity.key_fields`` minus the doc key (a chunk tag sends ``keys=[speech_id, chunk_id]``)."""

    doc_id: str
    keys: list[int] = Field(default_factory=list, max_length=16)
    labels: list[Label] = Field(default_factory=list, max_length=MAX_SAVE_ROWS)


class TagBatch(BaseModel):
    """A workflow's tags of a task's item, written as tag rows into the task's draft in ONE save. ``removes`` untags
    (deletes the deterministic tag rows). Author is stamped server-side; ``source``/``status`` are fixed for a human
    set. ``base_version`` is optional: an add never overwrites a row (it is insert-if-absent) and a removal names its
    row by its deterministic id."""

    adds: list[TagWrite] = Field(default_factory=list, max_length=MAX_SAVE_ROWS)
    removes: list[TagWrite] = Field(default_factory=list, max_length=MAX_SAVE_ROWS)
    base_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        # Each label of each write is a row the batch writes or removes, counted as a save counts its rows.
        rows = sum(len(w.labels) for w in [*self.adds, *self.removes])
        if rows > MAX_SAVE_ROWS:
            raise ValueError(f"a tag batch names {rows} tag rows, and one names at most {MAX_SAVE_ROWS}")
        return self


#: The columns of a unit's work as the canvas reads it (`annotator.annotations.work.wire_table`), aligned to the
#: engine's reader, with the active-learning columns (confidence/uncertainty/source/model_version) so a model's
#: proposals round-trip and the review queue can rank by them. ``links`` carries each row's outgoing relations
#: (``[{"name", "to_shape"}]``, the shape an import reads back) and ``metadata`` its attributes, both as JSON.
EMPTY_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        # The task whose work the row is: a unit several tasks hold (a consensus item's replicas, each an annotator's
        # own) is each one's work apart, its own draft.
        ("task_id", pa.string()),
        ("shape_type", pa.string()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("width", pa.float32()),
        ("height", pa.float32()),
        ("rotation", pa.float32()),
        ("polygon", pa.list_(pa.float32())),
        # temporal facet — audio segments + a shape pinned to a video moment (seconds)
        ("t_start", pa.float32()),
        ("t_end", pa.float32()),
        ("text", pa.string()),
        # TEXTUAL facet — a span INTO another row's `text`: `parent_id` is the row the span sits inside, by ID, never an
        # index (rows renumber on every reload), and -1 on both offsets is a row that is no span.
        ("parent_id", pa.string()),
        ("char_start", pa.int32()),
        ("char_end", pa.int32()),
        ("label", pa.string()),
        ("status", pa.string()),
        ("source", pa.string()),
        ("reviewer", pa.string()),
        ("confidence", pa.float32()),
        ("uncertainty", pa.float32()),
        ("model_version", pa.string()),
        ("group", pa.string()),
        ("difficult", pa.bool_()),
        # JSON, never an empty string (which is not JSON): `[]` for a row with no links, `{}` for one with no
        # attributes.
        ("links", pa.json_()),
        ("mask", pa.string()),
        ("metadata", pa.json_()),
        # row lifecycle — stamped server-side: both at row birth, updated_at again on every edit.
        ("created_at", pa.timestamp("us", tz="UTC")),
        ("updated_at", pa.timestamp("us", tz="UTC")),
    ]
)
