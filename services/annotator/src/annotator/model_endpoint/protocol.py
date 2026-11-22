"""The workbench's model protocol, version 1: what a model endpoint is asked and what it answers (ADR 0023, ADR 0030).

The workbench deploys no model. A deployment gives it the address of a model endpoint, and the workbench calls it by
this protocol; a backend that speaks another protocol is reached through an adapter that speaks this one. The prose is
`docs/model-protocol.md`, and `docs/model-protocol-openapi.json` is this module's models as the stand-in serves them
(`annotator.model_endpoint.stand_in`), so the document, the stand-in and the caller cannot disagree.

Three doors:

- ``GET /v1/models``: the models the endpoint serves and what each can do.
- ``POST /v1/proposals``: one model's proposals on one item.
- ``POST /v1/assessments``: what one model disagrees with among the labels people made on one item (ADR 0026).

Every call presents the caller's own bearer token (``Authorization``). An error is an HTTP status with an RFC 9457
problem body, never a 200 that says it failed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_VERSION = "1"

MODELS_PATH = "/v1/models"
PROPOSALS_PATH = "/v1/proposals"
ASSESSMENTS_PATH = "/v1/assessments"

#: The shape vocabulary a proposal is written in: the one a draft, an ontology and a published table speak. An adapter
#: maps its backend's names onto it; an answer naming another is outside the protocol.
ProposalShape = Literal["bbox", "polygon", "polyline", "keypoint", "mask", "tag", "text", "segment"]
#: What a model reads of an item.
ContentKind = Literal["image", "text"]
#: What a person may add to the ask: words, a box drawn on a picture, or clicks on it.
PromptKind = Literal["text", "box", "points"]

#: The most models one endpoint lists, proposals one answer carries and points one polygon names.
MAX_MODELS = 256
MAX_PROPOSALS = 2000
MAX_POLYGON_VALUES = 20_000
#: The longest prompt, and the most clicks, one ask carries.
MAX_PROMPT = 2000
MAX_POINTS = 64
#: The most classes one ask names.
MAX_CLASSES = 1000
#: The most labels one ask for an assessment carries, and the longest reason a disagreement gives.
MAX_LABELS = 2000
MAX_REASON = 500
#: The longest id an ask names a label by: the caller's own, which the workbench takes from the row it stores.
MAX_LABEL_ID = 4096
#: The largest picture an ask carries, in bytes before base64.
MAX_IMAGE_BYTES = 16 * 1024 * 1024
#: The longest text an ask carries, in characters.
MAX_TEXT = 1_000_000
#: A picture's media type.
IMAGE_TYPE_PATTERN = r"^image/[A-Za-z0-9.+-]{1,64}$"

#: What a model, and a version of one, is named by.
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/-]*"
Name = Annotated[str, Field(min_length=1, max_length=128, pattern=rf"^{NAME_PATTERN}$")]

#: Strict on both sides: a field the protocol does not name is refused, so a caller and an endpoint that drifted apart
#: fail loudly instead of dropping what the other said.
STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)


class ModelInfo(BaseModel):
    """One model an endpoint serves, and what it can do."""

    model_config = STRICT

    name: Name
    #: Which version of the model answers. Every proposal records it, so two versions can be compared.
    version: Name
    #: What a person is shown for it; empty shows the name.
    title: Annotated[str, Field(max_length=200)] = ""
    #: What it reads of an item. An ask carrying another kind is refused with 422.
    reads: Annotated[list[ContentKind], Field(min_length=1, max_length=8)]
    #: The prompts it takes. Empty: it answers from the item alone.
    prompts: Annotated[list[PromptKind], Field(max_length=8)] = Field(default_factory=list)
    #: The shape types its proposals carry. Empty: it proposes nothing, and an ask for proposals is refused with 422.
    returns: Annotated[list[ProposalShape], Field(max_length=16)] = Field(default_factory=list)
    #: The shape types of the labels it assesses. Empty: it judges nothing, and an ask for an assessment is refused
    #: with 422.
    assesses: Annotated[list[ProposalShape], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _does_something(self) -> ModelInfo:
        if not self.returns and not self.assesses:
            raise ValueError("a model that neither proposes nor assesses is not listed")
        return self


class ModelListing(BaseModel):
    """The answer of ``GET /v1/models``."""

    model_config = STRICT

    protocol: Literal["1"] = PROTOCOL_VERSION
    models: Annotated[list[ModelInfo], Field(max_length=MAX_MODELS)]


class ImageContent(BaseModel):
    """A picture, carried in the ask: the endpoint fetches nothing and needs no way back to the workbench."""

    model_config = STRICT

    kind: Literal["image"] = "image"
    mime: Annotated[str, Field(pattern=IMAGE_TYPE_PATTERN)]
    #: The picture's bytes, base64 (RFC 4648 §4).
    data: Annotated[str, Field(min_length=1, max_length=(MAX_IMAGE_BYTES * 4) // 3 + 4)]


class TextContent(BaseModel):
    """A text. Offsets a proposal names into it count code points."""

    model_config = STRICT

    kind: Literal["text"] = "text"
    text: Annotated[str, Field(max_length=MAX_TEXT)]


class Item(BaseModel):
    """The item proposals are asked on."""

    model_config = STRICT

    #: The caller's own name for the item, opaque to the endpoint: what its logs can be matched by.
    id: Annotated[str, Field(min_length=1, max_length=1024)]
    content: Annotated[ImageContent | TextContent, Field(discriminator="kind")]


class Box(BaseModel):
    """A box on a picture, in the picture's own pixels."""

    model_config = STRICT

    x: float
    y: float
    width: Annotated[float, Field(ge=0)]
    height: Annotated[float, Field(ge=0)]


class Point(BaseModel):
    """A click on a picture, in its pixels. ``positive: false`` says "not this"."""

    model_config = STRICT

    x: float
    y: float
    positive: bool = True


class Prompts(BaseModel):
    """What the person added to the ask. A model reads the kinds it lists in ``prompts`` and an ask carrying another
    kind is refused with 422."""

    model_config = STRICT

    text: Annotated[str, Field(max_length=MAX_PROMPT)] | None = None
    box: Box | None = None
    #: Every click of the session so far, in order: the ask is the whole session, and the endpoint keeps none.
    points: Annotated[list[Point], Field(max_length=MAX_POINTS)] = Field(default_factory=list)

    def kinds(self) -> set[PromptKind]:
        """The prompt kinds this ask carries."""
        carried: set[PromptKind] = set()
        if self.text:
            carried.add("text")
        if self.box is not None:
            carried.add("box")
        if self.points:
            carried.add("points")
        return carried


class ProposalsRequest(BaseModel):
    """The body of ``POST /v1/proposals``."""

    model_config = STRICT

    model: Name
    item: Item
    prompts: Prompts = Field(default_factory=Prompts)
    #: The classes the item's task labels with. A model that can label in a given vocabulary uses these names; one that
    #: cannot ignores them. Empty: the task names none.
    classes: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=MAX_CLASSES)] = Field(default_factory=list)
    #: The person the ask is made for, as the caller names them. For the endpoint's own record; it is not a credential,
    #: and the bearer token alone says who calls.
    requested_by: Annotated[str, Field(max_length=512)] = ""


class Proposal(BaseModel):
    """One proposed shape, in the item's own coordinates (a picture's pixels, a text's code points)."""

    model_config = STRICT

    shape_type: ProposalShape
    x: float = 0.0
    y: float = 0.0
    width: Annotated[float, Field(ge=0)] = 0.0
    height: Annotated[float, Field(ge=0)] = 0.0
    #: A polygon's or a polyline's points, flat: ``[x0, y0, x1, y1, ...]``.
    polygon: Annotated[list[float], Field(max_length=MAX_POLYGON_VALUES)] = Field(default_factory=list)
    #: The class the model proposes, one of the ask's ``classes`` where it labels in them; empty proposes none.
    label: Annotated[str, Field(max_length=128)] = ""
    #: A textual answer: a transcription, or a tag's value.
    text: Annotated[str, Field(max_length=MAX_TEXT)] = ""
    #: A text span's range, in code points; both absent on any other shape.
    char_start: Annotated[int, Field(ge=0)] | None = None
    char_end: Annotated[int, Field(ge=0)] | None = None
    #: How sure the model is, from 0 to 1. Required: a proposal with no score cannot be ranked or thresholded.
    score: Annotated[float, Field(ge=0.0, le=1.0)]

    @model_validator(mode="after")
    def _well_formed(self) -> Proposal:
        if len(self.polygon) % 2:
            raise ValueError("a polygon is pairs of x and y")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("a span names both char_start and char_end")
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("char_end is before char_start")
        return self


class ProposalsResponse(BaseModel):
    """The answer of ``POST /v1/proposals``: the model that answered, at which version, and what it proposes. No
    proposals is an answer ("nothing found"); a model that could not answer says so by its status."""

    model_config = STRICT

    model: Name
    version: Name
    proposals: Annotated[list[Proposal], Field(max_length=MAX_PROPOSALS)]


class AssessedLabel(BaseModel):
    """One label a person made or kept on the item, as the judge reads it: a proposal's geometry and words, under the
    caller's own name for it."""

    model_config = STRICT

    #: The caller's name for the label, opaque to the endpoint: what a disagreement names it by.
    id: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_ID)]
    shape_type: ProposalShape
    x: float = 0.0
    y: float = 0.0
    width: Annotated[float, Field(ge=0)] = 0.0
    height: Annotated[float, Field(ge=0)] = 0.0
    polygon: Annotated[list[float], Field(max_length=MAX_POLYGON_VALUES)] = Field(default_factory=list)
    label: Annotated[str, Field(max_length=128)] = ""
    text: Annotated[str, Field(max_length=MAX_TEXT)] = ""
    char_start: Annotated[int, Field(ge=0)] | None = None
    char_end: Annotated[int, Field(ge=0)] | None = None


class AssessmentRequest(BaseModel):
    """The body of ``POST /v1/assessments``: an item, and the labels people made on it."""

    model_config = STRICT

    model: Name
    item: Item
    #: Every label of the item, so the judge can also say one is missing. Empty: the person labelled nothing.
    labels: Annotated[list[AssessedLabel], Field(max_length=MAX_LABELS)]
    classes: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=MAX_CLASSES)] = Field(default_factory=list)
    requested_by: Annotated[str, Field(max_length=512)] = ""

    @model_validator(mode="after")
    def _names_each_label_once(self) -> AssessmentRequest:
        if len({label.id for label in self.labels}) != len(self.labels):
            raise ValueError("two labels of one ask carry the same id")
        return self


class Disagreement(BaseModel):
    """One thing the judge disagrees with: a label of the ask, or the item as a whole (a label that is missing)."""

    model_config = STRICT

    #: The label disagreed with, by the ask's id for it; absent when the disagreement is about the item as a whole.
    label_id: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_ID)] | None = None
    #: What the judge disagrees with, as a reviewer reads it. Required: a flag with no reason cannot be weighed.
    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON)]
    #: The class the judge would have given, one of the ask's ``classes``; empty suggests none.
    suggested_label: Annotated[str, Field(max_length=128)] = ""
    #: How sure the judge is of the disagreement, from 0 to 1.
    score: Annotated[float, Field(ge=0.0, le=1.0)]


class AssessmentResponse(BaseModel):
    """The answer of ``POST /v1/assessments``: the model that judged, at which version, and what it disagrees with. No
    disagreements is an answer ("the labels look right"); a judge that could not answer says so by its status."""

    model_config = STRICT

    model: Name
    version: Name
    disagreements: Annotated[list[Disagreement], Field(max_length=MAX_LABELS)]


class Problem(BaseModel):
    """An error's body (RFC 9457, ``application/problem+json``). The status says what happened:

    - 401: no bearer token, or one the endpoint does not accept.
    - 403: this caller may not ask this model.
    - 404: the endpoint serves no model of that name.
    - 413: the item is larger than the model takes.
    - 422: the ask is outside the protocol, or carries a content or prompt kind the model does not read.
    - 429, 503: busy or unavailable for now; ``Retry-After`` says when to ask again.
    - any other 5xx: the model failed.
    """

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str = ""
    status: int
    detail: str = ""


__all__ = [
    "ASSESSMENTS_PATH",
    "IMAGE_TYPE_PATTERN",
    "MAX_IMAGE_BYTES",
    "MAX_LABELS",
    "MAX_POINTS",
    "MAX_PROMPT",
    "MAX_REASON",
    "MAX_TEXT",
    "MODELS_PATH",
    "NAME_PATTERN",
    "PROPOSALS_PATH",
    "PROTOCOL_VERSION",
    "AssessedLabel",
    "AssessmentRequest",
    "AssessmentResponse",
    "Box",
    "ContentKind",
    "Disagreement",
    "ImageContent",
    "Item",
    "ModelInfo",
    "ModelListing",
    "Point",
    "Problem",
    "PromptKind",
    "Prompts",
    "Proposal",
    "ProposalShape",
    "ProposalsRequest",
    "ProposalsResponse",
    "TextContent",
]
