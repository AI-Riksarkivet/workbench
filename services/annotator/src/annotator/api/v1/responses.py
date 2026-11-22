"""The annotator's WIRE shapes — what each route publishes, declared.

Thirteen handlers were annotated `-> dict[str, Any]` with no `response_model` between them, so
`/openapi.json` described their answers as "an object" and whatever the stored document happened to
hold is what shipped. Two things follow from that, and the second is the
one that bites: the frontend's valibot schemas became the only statement of these shapes, on the far
side of the wire and maintained by hand; and a field added to `Task` or `AnnotationProject` for the
store's own bookkeeping is published to every client the moment it is stored.

**The envelopes live here; the documents do not.** Where a domain model already describes the
payload — `Task`, `Draft`, `AnnotationProject` — the route publishes THAT model rather than a
parallel copy of it, because a second description of the same document is a second thing to drift.
What this module adds is only the envelopes the routes build around them, which have no home in the
domain layer: they are shapes of the HTTP surface, not of the plane.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from annotator.judgement import Judgement
from annotator.projects.models import AnnotationProject, Draft, SamplingStrategy, Task
from annotator.sampling import ItemSampling
from annotator.state.task_query import Contributor, ItemWork


class LegalEvent(BaseModel):
    """One principal-fireable transition, straight from `machines.legal_*_events`.

    `permission` is the relation that gates it, so a UI can EXPLAIN a disabled action. The actual
    gate stays server-side — this is a description, never an authorization.
    """

    event: str
    to: str
    permission: str


class ProjectListing(BaseModel):
    """`GET /projects?tenant=` — the tenant's projects, oldest first."""

    projects: list[AnnotationProject]
    total: int


class ProjectDetail(BaseModel):
    """`GET /projects/{id}` — the project plus the edges a principal may fire out of its state."""

    project: AnnotationProject
    legal_events: list[LegalEvent]
    #: The project permissions the CALLER holds, of those its pages gate an action on (`projects.READER_PERMISSIONS`),
    #: from the workbench's store: a page offers an action only to someone its door would let through, and the door
    #: still checks on its own.
    permissions: list[str]


class TaskDetail(Task):
    """One task as the queue UI reads it: the document plus its own legal edges.

    Inherits `Task` rather than restating it — the extra key is the only difference, and a
    hand-copied twin of a 25-field document is a drift waiting to happen.
    """

    legal_events: list[LegalEvent] = Field(default_factory=list)
    #: Each principal the task names → the name they last wrote under, where one is kept (`annotator.people`). A single
    #: task's read carries it; a listing's details share the listing's `people` instead.
    people: dict[str, str] = Field(default_factory=dict)


class ListedTask(TaskDetail):
    """One item of a listing's page: a `TaskDetail`, with its work when the listing asked for it (`include=work`)."""

    #: The item's work as a bulk label reads it, or null where the reader may not read it; absent unless asked for (the
    #: listing route excludes what it did not set).
    work: ItemWork | None = None
    #: What the project's judge said of the item's submission of now (`include=judge`), or null where none has read it.
    judgement: Judgement | None = None
    #: What sampling knows of the item (`include=sampling`): its score by the project's sampling model of now, its
    #: novelty, and in a listing ordered by the strategy which placed it; null where it knows nothing.
    sampling: ItemSampling | None = None


class JudgeState(BaseModel):
    """A project's judge as a listing reads it (`include=judge`): the model it names, whether it can be asked and why
    not (no judge named, or a deployment with no model endpoint), and how it has read the items the filter matches. An
    endpoint that fails is learnt when it is asked (`POST /api/judge/...`), never here."""

    model: str
    available: bool
    reason: str
    flagged: int
    clear: int
    unread: int


class SamplingState(BaseModel):
    """A project's sampling as a listing reads it (`include=sampling`): its strategy and the model it scores by, whether
    the strategy can be followed and why not (`annotator.sampling.why_unavailable`), whether a model can be asked for a
    score (the project names one and the deployment an endpoint), and of the items the filter matches how many that
    model has scored and how many are measured against the labelled items. What a dataset lacks is learnt when it is
    read (`POST /projects/<id>/sampling/reads`), and an endpoint that fails when it is asked."""

    strategy: SamplingStrategy
    model: str
    available: bool
    reason: str
    asks: bool
    scored: int
    unscored: int
    measured: int
    unmeasured: int


class TaskListing(BaseModel):
    """`GET /projects/{id}/tasks` — the index and the publish precondition, and the page the filter asked for, from ONE
    snapshot.

    Every key past the first five is present only for the `include` that asks for it, and their ABSENCE is
    load-bearing: the route is declared `response_model_exclude_unset=True`, so the plain listing publishes exactly the
    four keys the index is.
    """

    counts: dict[str, int]
    total: int
    terminal: int
    may_publish: bool
    details: list[ListedTask] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: How many items the filter matches, and how many each state holds under every other filter.
    matched: int = 0
    state_counts: dict[str, int] = Field(default_factory=dict)
    #: Where the page starts in the filtered order, and where `around`'s item sits in it (None: the filter leaves it out).
    offset: int = 0
    position: int | None = None
    #: The datasets the project's items come from (what the filter by dataset offers).
    datasets: list[str] = Field(default_factory=list)
    contributors: list[Contributor] = Field(default_factory=list)
    judge: JudgeState | None = None
    sampling: SamplingState | None = None
    #: Each principal this answer names → the name they last wrote under (`annotator.people`), where one is kept.
    people: dict[str, str] = Field(default_factory=dict)


class SendReceipt(BaseModel):
    """`POST /projects/{id}/items` — what a send actually created.

    `created` counts the batch's per-task answers, not the send list: an item that was already
    indexed reports as not created, which is what makes a retried send readable.
    """

    sent: int
    created: int
    task_ids: list[str]


class DropReceipt(BaseModel):
    """`DELETE /projects/{id}/tasks/{task_id}` — idempotent, so `removed` may be false."""

    task_id: str
    removed: bool
    total: int


class DraftImport(BaseModel):
    """`POST /tasks/{id}/import` — what the Arrow-IPC import appended, plus the resulting draft."""

    imported: int
    links: int
    draft: Draft
