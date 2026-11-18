"""The annotation-project domain: four documents, two state machines, one publish record.

Server-side counterpart of `frontend/packages/labeling`'s `LabelOp` model. Spec:
`OPEN-WORK.md#design--annotation-projects` §4 (entities) and §5 (state machines).

Deliberately store-free and corpus-free. This module imports nothing from the lakehouse plane — no
`lancekit`, no registry, no dataset handle — because an annotation project's state (tasks, claims,
drafts, reviews) is the annotator's own and never enters the governed plane until a publish. The
import guard in `services/annotator/tests/test_annotation_projects_machine.py` makes that mechanical rather than a
promise; the documents are stored by `annotator.state.store`.

Ids are `uuid4().hex`, not UUID7: the deployed interpreter is CPython 3.13 (`uuid.uuid7` is absent)
and ordering comes from `created_at` and the task id, so a ULID dependency would buy nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from annotator.projects.ontology import LabelOntology
from annotator.text import MAX_ID, MAX_NAME, Text
from service_kit.naming import CONTROL_ID_PATTERN


def new_id() -> str:
    """A fresh document id (uuid4 hex, per §4)."""
    return uuid4().hex


class ProjectState(StrEnum):
    """§5.1. `publish_failed` is a distinct resting state so a retry is a legal edge, not a reset."""

    DRAFT = "draft"
    LABELING = "labeling"
    FROZEN = "frozen"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    ARCHIVED = "archived"


class TaskState(StrEnum):
    """§5.2 — one axis, six states. A task is in exactly one of these, always."""

    UNASSIGNED = "unassigned"
    CLAIMED = "claimed"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    ACCEPTED = "accepted"
    SKIPPED = "skipped"


#: Publishing requires EVERY task terminal (§5.1). A task in `in_review` blocks it — the owner's
#: "nothing lands before that", enforced rather than described.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset({TaskState.ACCEPTED, TaskState.SKIPPED})

SkipPolicy = Literal["requeue_for_others", "requeue_for_me", "terminal"]
ShapeType = Literal["bbox", "polygon", "mask", "keypoint", "polyline", "segment", "tag", "text"]
ReviewAction = Literal["accepted", "fix_and_accept", "request_changes"]
#: Where a draft's contents came from. `import` is its own member rather than being folded into
#: `model`: imported work may be a model's output OR a person's, made in another tool, and calling
#: a human's labels "model" would put a false provenance claim onto every row they eventually
#: publish. The distinction is the whole reason this field exists.
DraftOrigin = Literal["human", "model", "propagated", "import"]
#: `text` is a first-class modality: the document IS the canvas (token classification, NER over
#: transcriptions). It was absent here while the zone already shipped a text viewer — so an honest
#: text task 422'd at send and `kind=text` stayed a hand-edited URL no queue link could produce.
MediaKind = Literal["image", "audio", "video", "text"]


class MediaRef(BaseModel):
    """What kind of media an item is, and its size when known. It names no link: a task's picture is signed by the viewer
    from the item's key, on the reader's own read, and a link a sender stored would serve without it."""

    kind: MediaKind
    width: int | None = None
    height: int | None = None


class ItemSource(BaseModel):
    """The send capture (§4.5): what selection produced this task."""

    kind: Literal["chunks", "scope", "corpus"]
    keys: list[Text] = Field(default_factory=list)
    where: Text | None = None
    #: The version of `table` the sender was looking at when they sent the item (§4.5's reproducibility capture). The
    #: sender names it; the send keeps it only once the catalog confirms `table` has that version, as the sender reads it
    #: (`project_events._refuse_unheld_versions`), and drops it when the source names no table. Every kept
    #: (table, version) of the published items is an input the publish names (`publish.publish_inputs`); an item with
    #: none names none, and a target under the run rule then refuses the publish before it writes.
    dataset_version: int | None = Field(default=None, ge=1)
    #: The governed table `dataset_version` is a version of, written by the server at send and never taken from the
    #: sender: the source itself when it names a table, else its dataset's default row table as the catalog showed the
    #: sender (`annotator.corpus.input_tables`); None for a dataset that declares no search. Only this table is ever a
    #: publish's input.
    table: Text | None = None


#: The most keys one sent item's source names.
MAX_SOURCE_KEYS: Final = 64


class SentSource(ItemSource):
    """An item's source as a sender states it: the stored shape, held to the bounds of what comes in. A stored source is
    read as it was written, so the bounds are here only, where Pydantic names the field past one."""

    keys: list[Annotated[Text, Field(max_length=MAX_ID)]] = Field(default_factory=list, max_length=MAX_SOURCE_KEYS)
    where: Annotated[Text, Field(max_length=MAX_NAME)] | None = None


class Transition(BaseModel):
    """One audit row. The FSM is insert-only: a transition is never EDITED.

    It can be shed, though, and that is not the same thing: past :data:`MAX_TRANSITIONS` the OLDEST
    rows fall off the task document and `Task.transitions_dropped` counts them, because the document
    is rewritten whole on every write and an unbounded trail makes each event cost more than the
    last. The durable record of a transition is the audit/lineage plane; this list is the task's own
    recent history.
    """

    at: datetime
    by: str
    event: str
    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")

    #: `from` is a Python keyword, so the field is `from_state` and the WIRE name is the alias.
    #: `serialize_by_alias` makes the wire shape the default in both directions — without it a caller
    #: that forgets `by_alias=True` silently emits `from_state`/`to_state`, and the audit trail ends
    #: up with two shapes depending on which code path wrote the row.
    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class ReviewNote(BaseModel):
    """Reviewer feedback (§4.2), appended and never edited — bounded like `Transition`.

    Past :data:`MAX_REVIEW_NOTES` the oldest notes fall off and `Task.review_notes_dropped` counts
    them; what a returning annotator needs is the latest request, and the document is rewritten
    whole on every write.
    """

    by: str
    at: datetime
    action: ReviewAction
    message: str = ""
    shape_ids: list[str] = Field(default_factory=list)


class Shape(BaseModel):
    """One annotation. Carries its own provenance so the payload stays mode-blind."""

    shape_id: str = Field(default_factory=new_id)
    shape_type: ShapeType
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    rotation: float | None = None
    polygon: list[float] = Field(default_factory=list)
    t_start: float | None = None
    t_end: float | None = None
    #: TEXT SPAN. A `text` shape may be a range INTO another annotation's `text`: token-classification
    #: and the value half of KIE on transcribed text are spans, and no amount of pixel geometry
    #: expresses one. `parent_id` addresses the row by ID — rows renumber on reload, so an index would
    #: silently re-point at a different annotation.
    parent_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    mask: str | None = None
    label: str | None = None
    text: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    group: str | None = None
    difficult: bool = False
    source: str | None = None
    model_version: str | None = None
    confidence: float | None = None
    #: The canvas's review state of the row: `accepted` for drawn work, `prediction` for a model's proposal no person
    #: has kept yet, and whatever the reviewer sets. Its active-learning `uncertainty` rides beside `confidence`.
    status: str = "accepted"
    uncertainty: float | None = None
    #: Who last wrote the row and when it was made and last changed, stamped by the server on every save that touches
    #: it (`annotator.annotations.work`), never taken from the caller.
    reviewer: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Link(BaseModel):
    """One typed edge between two shapes in a draft — the structure KIE and DocVQA need.

    Key information extraction is a key→value linking problem and document VQA binds a question to
    the region that answers it. Neither is a per-shape property, so no amount of attribute typing
    reaches them. The ontology declares which links are LEGAL (`RelationClass`); this is one that
    was actually drawn.

    Endpoints are shape IDs, not indices: a draft is replaced whole on every save, and an index
    would silently re-point at a different shape the moment one is deleted.
    """

    name: str
    from_shape: str
    to_shape: str


class Draft(BaseModel):
    """ONE document per task holding its work in progress: the whole shape set of its item's unit as a single list
    (§4.3), and the links between them.

    It is the one store of work in progress: the canvas reads it and saves its changes to it (`annotator.annotations`),
    the submit judges it, the review and the compare read it, and only the publish writes it to the lakehouse. Not a
    row per shape: a save is one keyed write guarded by `revision`, which the canvas's door serves as
    `X-Annotations-Version`, so two tabs cannot silently lose each other's work (a mismatch is a 409). Every revision
    is kept in the bounded history (`annotator.state.store.MAX_DRAFT_HISTORY`).
    """

    task_id: str
    project_id: str
    author: str
    shapes: list[Shape] = Field(default_factory=list)
    #: The links drawn between those shapes. Absent from this model until 2026-08-04, which made an
    #: ontology declaring a REQUIRED relation unsatisfiable: the submit check read an always-empty
    #: list, so every submission that could ever be made was refused. Declared, enforced, and
    #: impossible — the worst of the three, and invisible because nothing errored on the way in.
    links: list[Link] = Field(default_factory=list)
    revision: int = 0
    updated_at: datetime | None = None
    origin: DraftOrigin = "human"


class Adjudication(BaseModel):
    """Consensus v1's manager merge — a PICK, never a blend.

    The manager names ONE accepted replica of a group as canonical; every replica's rows still
    publish, and the facet carries this pick with its attribution. Synthesizing a merged shape set
    would put words in annotators' mouths — a downstream consumer that wants the canonical rows
    filters by the picked `task_id`."""

    task_id: str
    by: str
    at: datetime


class PublishRecord(BaseModel):
    """Set ONCE, only by the publish workflow (§4.1)."""

    table_id: str
    namespace: str
    version: int
    publish_id: str
    published_at: datetime
    published_by: str


#: The bound on a task's two append-only histories.
#:
#: They used to have none, and the whole document is re-serialised on every write — so the cost of
#: an event was proportional to how many events preceded it, with the ceiling set by how long the
#: task lived. `claim`/`release` is a legal loop with nothing rate-limiting it above, and a review
#: can request changes and be re-submitted any number of times.
#:
#: The trail is kept, not discarded: the NEWEST rows survive (that is what a reader is asking for),
#: and what falls off the end is counted in `transitions_dropped` / `review_notes_dropped`. The
#: durable audit record of a transition is the lineage/audit plane, not this document.
MAX_TRANSITIONS: Final = 200
#: The longest title, description and instructions a project takes, and its longest claim lease (a week).
TITLE_MAX: Final = 200
DESCRIPTION_MAX: Final = 4_000
INSTRUCTIONS_MAX: Final = 20_000
LEASE_MAX_SECONDS: Final = 7 * 24 * 3600
MAX_REVIEW_NOTES: Final = 50


class Task(BaseModel):
    """§4.2. `lease_expires_at is None` while CLAIMED means manager-pinned — it never expires."""

    task_id: str = Field(default_factory=new_id)
    project_id: str
    state: TaskState = TaskState.UNASSIGNED
    assignee: str | None = None
    lease_expires_at: datetime | None = None
    source: ItemSource
    media: MediaRef
    #: Captured from the PROJECT at send time. It decides whether `submit` lands in `in_review` or
    #: straight in `accepted`, so it must never be a request field: an annotator who could pass it
    #: would self-accept and walk straight past review — the one guarantee this whole plane exists
    #: to provide.
    review_required: bool = True
    #: Also captured from the project at send time (§4.1 `lease_seconds`). The claim path reads THIS
    #: when the request names no duration — before the capture existed the project setting was
    #: stored and never read, and every claim ran on the client's default.
    lease_seconds: int = 1800
    #: Captured from the project at send (like `review_required`/`lease_seconds`): the ontology the
    #: SUBMIT is validated against, in the store's transition, for any caller. Capturing it is what makes the
    #: closed-set label check possible at all — enforcement happens here, so the taxonomy has to be
    #: HERE. It used to capture the `template` and leave the taxonomy on the project, which is why
    #: `label="asdf"` submitted and published.
    ontology: LabelOntology = Field(default_factory=LabelOntology)
    #: PRE-ANNOTATIONS attached at send — Label Studio's "prediction", and deliberately not a draft.
    #: A bulk action ("label these five hundred pages as `letter`") produces suggestions, not work:
    #: a draft is submittable and `save_draft` refuses a task that is not CLAIMED, so writing one as
    #: the other would manufacture work drawn by nobody and walk it into review. Stored here instead,
    #: on the task's own document, so it costs no extra write and cannot be mistaken for an answer.
    #:
    #: Server-written, like every other capture on this model: `send` validates each shape against
    #: THIS project's taxonomy before seeding anything and stamps `source`, so a sender can neither
    #: invent a class nor forge human provenance.
    prediction: list[Shape] = Field(default_factory=list)
    #: Consensus v1: the replica GROUP this item belongs to (the group id shared by its siblings),
    #: or None for an ordinary item. Sibling ids are deterministic (`{group}-r{k}`), which is what
    #: lets the one-replica-per-annotator guard find them without an index.
    replica_of: str | None = None
    #: Everyone who has ever held this task (claimed it, or been assigned it), in the order they first did. A release, a
    #: skip or a lapsed lease clears `assignee` but not this: on a replica it is what keeps a person who saw one
    #: replica's work off every sibling, and it is never trimmed, since it holds each person once.
    holders: list[str] = Field(default_factory=list)
    #: When the item was sent into the project, on the database's clock: stamped by the store's send, never by the sender.
    sent_at: datetime | None = None
    submitted_by: str | None = None
    submitted_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: ReviewAction | None = None
    review_notes: list[ReviewNote] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    #: How many of each the document has ALREADY shed to :data:`MAX_TRANSITIONS` /
    #: :data:`MAX_REVIEW_NOTES`. A bounded trail that does not say it is bounded is a trail that
    #: lies: a reader seeing 200 transitions cannot otherwise tell "this task moved 200 times" from
    #: "this task moved 8000 times and you are looking at the last 200".
    transitions_dropped: int = 0
    review_notes_dropped: int = 0
    lead_time_seconds: float = 0.0
    skipped_reason: str | None = None

    def trim_history(self) -> None:
        """Shed the oldest history past the caps, counting what went.

        Called from the store's single task-write seam rather than at each `append`, so a future
        append site inherits the bound instead of having to remember it.
        """
        if len(self.transitions) > MAX_TRANSITIONS:
            self.transitions_dropped += len(self.transitions) - MAX_TRANSITIONS
            self.transitions = self.transitions[-MAX_TRANSITIONS:]
        if len(self.review_notes) > MAX_REVIEW_NOTES:
            self.review_notes_dropped += len(self.review_notes) - MAX_REVIEW_NOTES
            self.review_notes = self.review_notes[-MAX_REVIEW_NOTES:]


class ProjectSettings(BaseModel):
    """A change of a project's settings: each field named replaces its value, each absent one is left as it is.

    The bounds are the create's (`CreateProjectRequest`). When each may change is the store's rule
    (`AnnotatorStore.update_settings`), stated where the app offers them.
    """

    model_config = {"extra": "forbid"}

    title: Annotated[Text, Field(max_length=TITLE_MAX)] | None = None
    description: Annotated[Text, Field(max_length=DESCRIPTION_MAX)] | None = None
    instructions: Annotated[Text, Field(max_length=INSTRUCTIONS_MAX)] | None = None
    review_required: bool | None = None
    lease_seconds: int | None = Field(default=None, gt=0, le=LEASE_MAX_SECONDS)
    consensus_n: int | None = Field(default=None, ge=1, le=5)


def publish_warehouse_of(project: Mapping[str, object]) -> str:
    """The warehouse a publish of a stored project document lands in: its own, else the one named like its tenant."""
    return str(project.get("warehouse") or project.get("tenant") or "")


class AnnotationProject(BaseModel):
    """§4.1. `tenant` is the authz parent — the object a role is checked on is `project:<project_id>`, and creating one
    is checked as `can_create_project` on `org:<tenant>`, in the workbench's own store."""

    project_id: str = Field(default_factory=new_id)
    tenant: str
    #: The lakehouse warehouse this project's publishes land in (`<warehouse>$<ANNOTATOR_PUBLISH_NAMESPACE>` unless a publish
    #: names another namespace in it). A rask project can hold
    #: several warehouses, so the warehouse is the project creator's choice; empty means the warehouse named like the
    #: tenant, which is the one rask's tenant set-up makes for a project (`docs/rask/COMMIT-EVENTS.md`).
    warehouse: str = Field(default="", pattern=rf"^(?:{CONTROL_ID_PATTERN})?$")
    slug: str
    title: str = ""
    description: str = ""
    #: Annotator-facing labeling instructions (how to label), distinct from `description` (what and
    #: why). Rendered on the detail page and the canvas handoff — the LS-parity "instructions page".
    instructions: str = ""
    state: ProjectState = ProjectState.DRAFT
    review_required: bool = True
    lease_seconds: int = 1800
    #: Consensus v1: how many INDEPENDENT annotators label each sent item. N>1 makes `send` seed N
    #: replica items per source item (`Task.replica_of` groups them); one annotator may hold at
    #: most one replica of a group. The publish emits every accepted replica's rows and reports
    #: agreement COUNTS in the run facet — merging is a manager step, deliberately not built yet.
    consensus_n: int = Field(default=1, ge=1, le=5)
    #: The whole labeling task definition — taxonomy, per-class tools, attributes and relations.
    #: Replaces the `label_schema` + `template` PAIR, which nothing cross-checked: the published
    #: facet stamped its class list from one and enforcement read the other, so a project could
    #: declare polygon classes and refuse every polygon at submit. Captured onto every item at send,
    #: and enforcement reads the ITEM's copy, so a mid-flight edit cannot retroactively invalidate
    #: work already in review.
    ontology: LabelOntology = Field(default_factory=LabelOntology)
    skip_policy: SkipPolicy = "requeue_for_others"
    #: Consensus v1's merge step: replica group id → the manager's canonical pick. Pre-publish
    #: metadata (re-pickable while the project is adjudicable); the publish validates every pick
    #: still names an ACCEPTED replica and stamps the map into the run facet.
    adjudications: dict[str, Adjudication] = Field(default_factory=dict)
    counts: dict[TaskState, int] = Field(default_factory=dict)
    lead_time_seconds_total: float = 0.0
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    published: PublishRecord | None = None
    publish_error: str | None = None
    #: The idempotency token for the publish saga, minted at the `publish` transition and REUSED by
    #: every retry. It exists so the saga's non-idempotent step — creating the table — becomes
    #: idempotent by key: the table id is derived from it, so a retry after a crash either finds the
    #: table it already created or creates it, but never makes a second one. `PublishRecord.publish_id`
    #: is the same value, copied on success; this field is what a RETRY reads, and it must therefore
    #: outlive a failure rather than being cleared with `publish_error`.
    pending_publish_id: str | None = None
    #: The publish INSTANT, minted with the token and reused by every retry — for the same reason.
    #: `published_at` is written into every published row and into `PublishRecord`, so stamping it
    #: per ATTEMPT would make a retried publish produce different rows than the attempt that crashed,
    #: and the "a replay is byte-identical" property the whole idempotency argument rests on would be
    #: false in exactly the case it is supposed to cover.
    pending_publish_at: datetime | None = None
    #: The TARGET namespace, pinned with the token. The endpoint authorized the publish against this
    #: namespace (§6.2 door 2), and the table id derives from it — so a crash-recovered saga reads it
    #: here rather than guessing, and a retry naming a different namespace is refused (a different
    #: namespace means a different table id, i.e. a second table for one logical publish).
    pending_target_namespace: str | None = None
    #: Who is driving the publish — re-stated on every `publish` fire (a retry may be driven by
    #: someone else, and `PublishRecord.published_by` should name the person whose action produced
    #: the publish that succeeded). Rows are unaffected: no published column carries this value, so
    #: updating it per retry does not break the byte-identical-replay property.
    pending_publish_by: str | None = None
    #: The saga's current step while PUBLISHING — A4's "surface where a publish is, not just a
    #: spinner". Written by `note_progress`, cleared when a publish starts (retry) or succeeds; on
    #: `publish_failed` it is KEPT: it names the step that was running when the attempt died.
    publish_progress: str | None = None

    @property
    def authz_object(self) -> str:
        """The object this project's roles are checked on."""
        return f"project:{self.project_id}"

    @property
    def publish_warehouse(self) -> str:
        """The warehouse a publish of this project lands in."""
        return publish_warehouse_of({"warehouse": self.warehouse, "tenant": self.tenant})

    @property
    def org_object(self) -> str:
        """The create-on-parent door: the project's organisation, checked before the child exists."""
        return f"org:{self.tenant}"
