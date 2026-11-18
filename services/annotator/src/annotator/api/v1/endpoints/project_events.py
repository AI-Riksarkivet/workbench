"""Project lifecycle endpoints — open, send, freeze, publish, archive (§5.1).

The sibling of `tasks.py`, and it reads the permission the same way: from `PROJECT_EDGES`, never from
a ladder written here. Adding an edge to the machine gates it with no route change, and a route
cannot drift from the model.

**Publish crosses into the lakehouse, and each side decides its own half.** The annotator checks
`can_publish` on `project:<project_id>` in the workbench's own store; the publish then writes as the
workbench's own identity, and the lakehouse decides whether that identity may create the table in the
target namespace (and, under the run rule the namespace may declare, whether the run names its inputs). The
target is a namespace of the project's own warehouse. The person needs no lakehouse write grant, and the commit
names them as the run's originator.

The third precondition — every task terminal — is NOT checked here. It lives in the state store's `publish`
transition (`annotator.state.store`), on the task rows its transaction holds, so it holds for any caller.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import AwareDatetime, BaseModel, Field
from starlette.concurrency import run_in_threadpool

from annotator.api.dependencies import PublishRunnerDep, StoreDep
from annotator.api.security import AuthorizerDep, CallerDep, Checker, CheckerDep, CurrentSubject, require_view, unseen_project
from annotator.api.v1.responses import DropReceipt, SendReceipt, TaskListing
from annotator.audit_actions import AnnotatorAuditAction
from annotator.core.config import get_annotator_settings
from annotator.corpus import input_tables, not_annotatable, unheld_versions
from annotator.people import RECORDS_PERSON
from annotator.projects.machines import (
    FROZEN_PROJECT_STATES,
    PROJECT_EDGES,
    PROJECT_SEND_STATES,
    IllegalTransition,
    legal_task_events,
    project_transition,
)
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, SentSource, Shape, ShapeType, Task, TaskState, new_id
from annotator.projects.ontology import ShapeLike, membership_violation
from annotator.projects.publish import publish_namespace, require_in_warehouse
from annotator.state.store import AnnotatorStore
from annotator.state.task_query import MAX_FILTER, MAX_OFFSET, DateField, SortOrder, TaskFilter, TaskSort, principals_in
from annotator.text import MAX_MASK, MAX_TEXT, Id, Name, Text
from client_kit.media.deps import StateDep
from client_kit.media.source_gate import require_read
from client_kit.media.state import AppState
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError, ServiceUnavailableError, ValidationError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.fga import Grant
from service_kit.governed.token_exchange import LakehouseCaller


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/projects", tags=["annotation-projects"], dependencies=RECORDS_PERSON)

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: Project edges caused by the SYSTEM (the publish saga), never by a principal. Refused on the HTTP
#: surface: `PROJECT_EDGES` giving them no permission means "no principal fires this",
#: and exposing them would let anyone mark another operator's publish succeeded.
SYSTEM_ONLY_EVENTS: Final[frozenset[str]] = frozenset(e for (_s, e), (_t, p) in PROJECT_EDGES.items() if p is None)

#: The most tasks one send creates, replicas included.
SEND_CAP: Final[int] = 1000


class ProjectEventRequest(BaseModel):
    """One event against a project. `event` must be an edge in `PROJECT_EDGES`.

    Carries no tenant: the authorization object is `project:<project_id>` from the path,
    so a caller cannot name the object its own permission is checked against.
    """

    event: Annotated[Text, Field(min_length=1, max_length=64)]
    #: Empty means the project's default target (`<warehouse>$<ANNOTATOR_PUBLISH_NAMESPACE>`), resolved by the door
    #: once it has loaded the project; a named one must be a namespace of the project's own warehouse.
    target_namespace: Name = ""


class PredictionShape(BaseModel):
    """One pre-annotation as a SENDER may state it — `Shape` minus its provenance.

    Declared separately rather than reusing `Shape` for the same reason `SendItem` is not a `Task`:
    `Shape` carries `source`, and `source` is how every later surface tells suggested work from
    drawn work. A sender able to set it could stamp `human` on five hundred items nobody looked at,
    and they would read as annotated for the rest of the corpus's life. Omitting the field is
    stronger than overwriting it — there is no path by which it can arrive.

    Unknown keys are IGNORED (pydantic's default), so a client sending a whole `Shape` is not an
    error; the fields it may not write simply do not land.
    """

    #: The stored shape's own type, so a prediction the door takes is always a `Shape` the send can build.
    shape_type: ShapeType
    label: Annotated[Text, Field(max_length=128)] | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    rotation: float | None = None
    polygon: list[float] = Field(default_factory=list)
    t_start: float | None = None
    t_end: float | None = None
    parent_id: Id | None = None
    char_start: int | None = None
    char_end: int | None = None
    mask: Annotated[Text, Field(max_length=MAX_MASK)] | None = None
    text: Annotated[Text, Field(max_length=MAX_TEXT)] | None = None
    attributes: dict[Annotated[Text, Field(max_length=64)], Annotated[Text, Field(max_length=4096)]] = Field(default_factory=dict, max_length=64)
    group: Annotated[Text, Field(max_length=128)] | None = None
    #: A producer's OWN confidence. Unlike `source` this is the sender's to state — it describes the
    #: suggestion, not who made it — and the uncertainty selector (`open_browse.md` §4) reads it.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model_version: Annotated[Text, Field(max_length=128)] | None = None


class SendItem(BaseModel):
    """One item to send. Deliberately NOT a `Task`.

    Accepting a full `Task` would let the sender supply `state`, `submitted_by`, `reviewed_by` and
    `review_action` — fabricating reviewed work with forged provenance, and defeating the entire
    "attribution comes from the task, not the payload" guarantee, because the client would have
    written the task. Only the fields that describe WHAT to annotate are accepted; everything about
    who did what is server-written — including a prediction's `source`, which is why the shapes
    below are `PredictionShape` and not `Shape`.
    """

    task_id: Annotated[Text, Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | None = None
    source: SentSource
    media: MediaRef
    #: PRE-ANNOTATIONS for this item — the bulk-labeling payload. Empty for an ordinary send. Capped
    #: because a prediction rides the task document: an unbounded list is an unbounded row.
    prediction: list[PredictionShape] = Field(default_factory=list, max_length=200)


class SendItemsRequest(BaseModel):
    """Send items into a project as tasks (§5.1: legal in draft/labeling, state unchanged)."""

    items: list[SendItem] = Field(min_length=1, max_length=SEND_CAP)


async def _check(checker: Checker, subject: str, relation: str, obj: str, what: AnnotatorAuditAction, **context: str) -> None:
    """One relation on one object, fail-closed, audited either way."""
    if not await checker(user=subject, relation=relation, obj=obj):
        audit(what, AuditOutcome.FAILURE, subject=subject, resource=obj, relation=relation, **context)
        raise ForbiddenError(f"{subject} lacks {relation} on {obj}")


async def _project(store: AnnotatorStore, project_id: str) -> AnnotationProject:
    project = await store.get_project(project_id)
    if project is None:
        raise ConflictError(f"annotation project {project_id} does not exist")
    return project


@router.post("/{project_id}/events", status_code=status.HTTP_200_OK)
async def fire_project_event(
    project_id: ProjectId, payload: ProjectEventRequest, checker: CheckerDep, subject: CurrentSubject, store: StoreDep, runner: PublishRunnerDep
) -> AnnotationProject:
    """Drive one project transition.

    The permission comes from `PROJECT_EDGES`; `publish` additionally crosses the lakehouse doors and, once the
    transition commits, starts the saga.
    """
    obj = f"project:{project_id}"
    # VIEW FIRST, before the project is read: which relation an event needs depends on the project's state, and every
    # answer below (absent, illegal from this state, no publish warehouse, the relation a 403 names) says something of
    # it. Someone who may not view the project gets one refusal whatever the project is.
    await require_view(checker, subject, obj, unseen_project(project_id))
    if payload.event in SYSTEM_ONLY_EVENTS:
        raise ForbiddenError(f"{payload.event} is fired by the publish saga, not by a principal, and cannot be posted over HTTP")

    current = await _project(store, project_id)
    try:
        _target, permission = project_transition(current.state, payload.event)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    # RESOLVED ONCE, here, and pinned with the publish token, so the write lands in the namespace this request named.
    target: str | None = None
    if payload.event == "publish":
        warehouse = current.publish_warehouse
        try:
            target = publish_namespace(warehouse, get_annotator_settings().publish_namespace)
        except ValueError as exc:
            raise ConflictError(f"annotation project {project_id} names no publish warehouse: {exc}") from exc
        if payload.target_namespace:
            try:
                target = require_in_warehouse(payload.target_namespace, warehouse)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        await _check(checker, subject, "can_publish", obj, AnnotatorAuditAction.PROJECT_PUBLISH)
        # Labeling needs no lakehouse, and a publish is the one thing that does: refused here, before the project moves,
        # so its work stays as it is and the reason is the answer to the click.
        if get_annotator_settings().sources.lakehouse is None:
            raise ConflictError(
                f"annotation project {project_id} cannot be published: this deployment reads no rask lakehouse, so there is nowhere to publish to. "
                "Its work stays here until a lakehouse is declared among the sources."
            )
    elif permission is not None:
        await _check(checker, subject, permission, obj, AnnotatorAuditAction.PROJECT_TRANSITION, event=payload.event)

    try:
        updated = await store.fire_project(project_id, event=payload.event, actor=subject, target_namespace=target)
    except IllegalTransition as exc:
        # The store's own preconditions — every task terminal, and a non-empty project.
        raise ConflictError(str(exc)) from exc

    if payload.event == "publish":
        runner.spawn(project_id)
    audit(AnnotatorAuditAction.PROJECT_TRANSITION, AuditOutcome.SUCCESS, event=payload.event, subject=subject, resource=project_id)
    return updated


class AdjudicationRequest(BaseModel):
    """The manager's pick for one replica group — just the winning replica's id. The group comes
    from the path and the picker from the verified subject, so neither can be forged in the body."""

    task_id: Annotated[Text, Field(min_length=1, max_length=80)]


GroupId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


async def _adjudicate(store: AnnotatorStore, checker: Checker, subject: str, project_id: str, group_id: str, task_id: str | None) -> AnnotationProject:
    await _check(checker, subject, "can_manage", f"project:{project_id}", AnnotatorAuditAction.PROJECT_ADJUDICATE)
    try:
        updated = await store.adjudicate(project_id, group=group_id, task_id=task_id, actor=subject)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    audit(AnnotatorAuditAction.PROJECT_ADJUDICATE, AuditOutcome.SUCCESS, subject=subject, resource=project_id)
    return updated


@router.put("/{project_id}/adjudications/{group_id}", status_code=status.HTTP_200_OK)
async def adjudicate_group(
    project_id: ProjectId, group_id: GroupId, payload: AdjudicationRequest, checker: CheckerDep, subject: CurrentSubject, store: StoreDep
) -> AnnotationProject:
    """Consensus v1's merge step: name ONE accepted replica of the group canonical (a pick, never a
    blend — every replica's rows still publish; the facet carries the pick with attribution).

    `can_manage`, not `can_review`: adjudication decides which OPINION wins, which is the manager's
    distribution authority, not a review of any single submission. PUT because re-picking while the
    project is adjudicable is the intended idempotent shape; the store refuses once provenance is
    frozen, and refuses a target that is not an accepted member of the group.
    """
    return await _adjudicate(store, checker, subject, project_id, group_id, payload.task_id)


@router.delete("/{project_id}/adjudications/{group_id}", status_code=status.HTTP_200_OK)
async def clear_adjudication(project_id: ProjectId, group_id: GroupId, checker: CheckerDep, subject: CurrentSubject, store: StoreDep) -> AnnotationProject:
    """Withdraw the pick for one group. Exists because a pick has no other exit: the publish
    refuses a stale or groupless pick, so without removal one wrong pick would wedge the publish.
    Idempotent — clearing an absent pick is a no-op — and refused once provenance is frozen."""
    return await _adjudicate(store, checker, subject, project_id, group_id, None)


@router.delete("/{project_id}/tasks/{task_id}", status_code=status.HTTP_200_OK)
async def drop_task(
    project_id: ProjectId,
    task_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    checker: CheckerDep,
    subject: CurrentSubject,
    store: StoreDep,
) -> DropReceipt:
    """Remove one item from a project, with its draft.

    Exists because a send can put items into a project that can NEVER be completed (an item naming a dataset that has
    since been renamed or removed cannot be opened, claimed or skipped), and the publish precondition requires every
    task terminal.

    `can_manage`, not the annotator's own permission: discarding work is a manager act. Idempotent —
    dropping an absent task is a no-op, so a retry cannot 404 a project that is already how the
    caller wants it.
    """
    await _check(checker, subject, "can_manage", f"project:{project_id}", AnnotatorAuditAction.PROJECT_DROP_TASK)
    if await store.get_project(project_id) is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    try:
        result = await store.drop_task(project_id, task_id)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    audit(AnnotatorAuditAction.PROJECT_DROP_TASK, AuditOutcome.SUCCESS, subject=subject, resource=project_id)
    return DropReceipt.model_validate(result.model_dump())


def _refuse_unknown_datasets(state: AppState, payload: SendItemsRequest, caller: LakehouseCaller) -> dict[str, str | None]:
    """Refuse the WHOLE send if any item names a media dataset that does not resolve; else each source's input table.

    Removal (`DELETE .../tasks/{id}`) is the escape hatch; this is the thing that stops the trap
    being set. An item naming a dataset that was renamed or removed cannot be opened on the canvas,
    so it can never be claimed, submitted or skipped — and the publish precondition requires EVERY
    task terminal, so one of them wedges the project. Creating it is the mistake; refusing at send
    is where it costs nothing.

    The catalog is asked, as the sender (`annotator.corpus.input_tables`): a dataset or table the sender cannot see is
    one their items could never open, and each one it shows names the governed table its captured version is of, which
    the send stamps on the item (`ItemSource.table`).

    An UNVERIFIABLE source refuses the send (503): the check is whether the sender may read what they send, and a check
    that did not run proves nothing (zero trust fails closed). The sender tries again once the catalog answers.
    """
    if read_only := not_annotatable(state, sorted({item.source.where or state.settings.default_dataset or "" for item in payload.items} - {""})):
        raise ConflictError("nothing was sent: " + "; ".join(read_only))
    names = sorted({item.source.where for item in payload.items if item.source.where})
    if not names:
        return {}  # every item takes the backend default, which resolves by construction
    found = input_tables(state, names, caller)
    if found is None:
        raise ServiceUnavailableError("the catalog could not be consulted about the items' datasets, so nothing was sent: try again")
    if unseen := [name for name in names if name not in found]:
        raise ConflictError(f"dataset(s) {unseen} do not exist or cannot be read — an item naming one could never be opened, claimed or completed")
    return found


def _refuse_unheld_versions(state: AppState, payload: SendItemsRequest, tables: dict[str, str | None], caller: LakehouseCaller) -> None:
    """Refuse the WHOLE send (409) when an item names a version its table does not have.

    The version an item carries is the sender's: the row table's version they were looking at, which the publish then
    names as its run's input. So the server holds it to the catalog before anything is written: the table the send
    stamps (`ItemSource.table`) must have that version, as the sender reads it. A version of an item whose source names
    no table is never an input, and the send drops it. Unverifiable is a 503, as `_refuse_unknown_datasets` is.
    """
    captured = {
        (table, item.source.dataset_version)
        for item in payload.items
        if item.source.dataset_version is not None and (table := tables.get(item.source.where or "")) is not None
    }
    if not captured:
        return
    unheld = unheld_versions(state, captured, caller)
    if unheld is None:
        raise ServiceUnavailableError("the catalog could not be consulted about the items' versions, so nothing was sent: try again")
    if unheld:
        raise ConflictError(f"the catalog holds no {', '.join(unheld)}: send the items again from what the dataset shows now")


#: What `send` stamps onto a pre-annotation's `source`: a free-form provenance string written by the SERVER, which
#: `publish` carries through verbatim. "bulk" is the honest word — a human chose the label, but chose it for a selection
#: rather than for this item.
BULK_SOURCE: Final[str] = "bulk"


def _validated_predictions(project: AnnotationProject, payload: SendItemsRequest) -> list[list[Shape]]:
    """Every item's pre-annotations, checked against the project's taxonomy and stamped.

    Returns one list per item, positionally aligned with ``payload.items``. Raises before returning
    if ANY item violates, so the caller can write knowing the whole send is legal.

    The taxonomy check is `membership_violation` — the same function import and submit use, shared
    rather than reimplemented so the three stages cannot drift. A bulk action is the worst possible
    place to leak an invented label: one click is five hundred rows.
    """
    out: list[list[Shape]] = []
    for index, item in enumerate(payload.items):
        # `source` is set HERE and nowhere else: `PredictionShape` does not declare it.
        shapes = [Shape(**shape.model_dump(), source=BULK_SOURCE) for shape in item.prediction]
        violation = membership_violation(project.ontology, [ShapeLike.model_validate(s.model_dump()) for s in shapes])
        if violation is not None:
            raise ConflictError(f"item {index + 1} of {len(payload.items)}: {violation}")
        out.append(shapes)
    return out


@router.post("/{project_id}/items", status_code=status.HTTP_201_CREATED)
async def send_items(
    caller: CallerDep,
    project_id: ProjectId,
    payload: SendItemsRequest,
    state: StateDep,
    checker: CheckerDep,
    authz: AuthorizerDep,
    subject: CurrentSubject,
    store: StoreDep,
) -> SendReceipt:
    """Send items into the project as tasks, in one transaction.

    Idempotent on the task id: an item already in this project reports as not created, and one whose id belongs to
    another project refuses the whole send, writing nothing.
    """
    # The roles first: the refusals below say whether the project exists and which state it is in.
    await require_view(checker, subject, f"project:{project_id}", unseen_project(project_id))
    await _check(checker, subject, "can_send_items", f"project:{project_id}", AnnotatorAuditAction.PROJECT_SEND)
    named = {item.source.where or state.settings.default_dataset or "" for item in payload.items} - {""}
    await require_read(state.settings.sources, checker, subject, sorted(named))
    project = await _project(store, project_id)
    if project.state not in PROJECT_SEND_STATES:
        # Checked here as well as in the store so the catalog is not asked about a send that cannot land.
        raise ConflictError(f"project {project_id} is {project.state} — items may only be sent while it is draft or labeling")

    # REFUSE an item whose media dataset does not resolve. Deliberately after the role check: the refusal names
    # datasets, which an unauthorised caller must not be able to enumerate. Off the loop: the catalog is asked
    # synchronously per named dataset.
    tables = await run_in_threadpool(_refuse_unknown_datasets, state, payload, caller)
    await run_in_threadpool(_refuse_unheld_versions, state, payload, tables, caller)

    # Consensus v1: N>1 makes N independent replicas per source item. Refused here early on the project as read; the
    # store makes the replicas and checks the cap again on the project its transaction holds.
    if len(payload.items) * project.consensus_n > SEND_CAP:
        raise ConflictError(f"{len(payload.items)} items × consensus_n={project.consensus_n} exceeds the {SEND_CAP}-task send cap — split the send")

    # Every prediction validated against the project's taxonomy and stamped BEFORE anything is written: a half-applied
    # bulk action is worse than a refused one. The store judges them again against the ontology it holds.
    predictions = _validated_predictions(project, payload)

    # One task per item, built from the two client-supplied descriptive fields. Every provenance and state field takes its
    # model default, so a sender cannot pre-set `state=accepted` or name another annotator; what the task captures from
    # the project (review, lease, ontology, its replicas) the store decides on the row it locks (`send_tasks`).
    tasks: list[Task] = []
    for index, item in enumerate(payload.items):
        # The input table is the server's to name, whatever the sender put there, and a version is kept only as a version
        # of that table, which `_refuse_unheld_versions` checked the catalog holds.
        table = tables.get(item.source.where or "")
        source = ItemSource.model_validate(item.source.model_dump() | {"table": table, "dataset_version": item.source.dataset_version if table else None})
        # Every replica of a group gets the SAME suggestion: consensus asks several people the same question.
        tasks.append(Task(task_id=item.task_id or new_id(), project_id=project_id, source=source, media=item.media, prediction=predictions[index]))

    try:
        created = await store.send_tasks(project_id, tasks, cap=SEND_CAP)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    # BIND each corpus the items come from to this project (`corpus:<dataset>#project`), so the people who annotate
    # here may write that corpus's annotations through the workbench (`can_annotate` on the corpus). The send's own
    # `can_send_items` is the model's `can_bind_corpus`, so no second check is asked. Written once the tasks are, and
    # idempotent, so a send retried after a failure here binds what the first one could not.
    if authz is not None:
        corpora = sorted({item.source.where or state.settings.default_dataset or "" for item in payload.items} - {""})
        await authz.write(Grant(user=f"project:{project_id}", relation="project", object=f"corpus:{name}") for name in corpora)

    audit(AnnotatorAuditAction.PROJECT_SEND, AuditOutcome.SUCCESS, subject=subject, resource=project_id)
    return SendReceipt(sent=len(payload.items), created=len(created), task_ids=created)


class TaskInclude(StrEnum):
    """What extra payload the task listing carries. A closed set makes a typo a 422 naming the legal values instead of a
    200 carrying the wrong shape."""

    #: A page of the tasks' whole documents, each with its legal events, and the page's counts.
    DETAILS = "details"
    #: Each person's part in the project over all its items.
    CONTRIBUTORS = "contributors"


#: The most principals one answer names (`people`): a page of 200 items names a few people in practice.
MAX_NAMED: Final = 1000

TextFilter = Annotated[Text | None, Query(min_length=1, max_length=MAX_FILTER)]


# `exclude_unset`: the plain listing publishes FOUR keys; each `include` adds its own.
@router.get("/{project_id}/tasks", response_model_exclude_unset=True)
async def list_project_tasks(
    project_id: ProjectId,
    checker: CheckerDep,
    subject: CurrentSubject,
    store: StoreDep,
    include: Annotated[list[TaskInclude] | None, Query(max_length=len(TaskInclude))] = None,
    state: Annotated[list[TaskState] | None, Query(max_length=len(TaskState))] = None,
    assignee: TextFilter = None,
    reviewer: TextFilter = None,
    submitter: TextFilter = None,
    not_submitter: TextFilter = None,
    holder: TextFilter = None,
    label: TextFilter = None,
    dataset: TextFilter = None,
    group: TextFilter = None,
    replicas: bool = False,
    date: DateField = DateField.UPDATED,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    sort: TaskSort = TaskSort.ITEM,
    order: SortOrder = SortOrder.ASC,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    around: TextFilter = None,
) -> TaskListing:
    """The task index plus the publish precondition, and with ``include=details`` one filtered, sorted page of the items.

    The index and the precondition are read over every task whatever the filter (a page that changed `may_publish`
    would be a far worse bug than an unpaged listing). ``include=details`` filters, orders and pages in Postgres
    (`annotator.state.task_query`): ``matched`` items match, ``state_counts`` is what each state would leave under the
    other filters, ``offset`` is where the page starts, and with ``around`` the page is the one holding that item near its
    middle, at ``position``. Each item comes with its ``legal_events`` (`machines.legal_task_events`), the single source
    the queue renders its actions from; ``include=contributors`` adds each person's part over all items. Every principal
    the answer names is in ``people`` with the name they last wrote under, where one is kept (`annotator.people`).
    """
    await require_view(checker, subject, f"project:{project_id}", unseen_project(project_id))
    includes = set(include or [])
    filters = TaskFilter(
        states=list(dict.fromkeys(state or [])),
        assignee=assignee,
        reviewer=reviewer,
        submitter=submitter,
        not_submitter=not_submitter,
        holder=holder,
        label=label,
        dataset=dataset,
        group=group,
        replicas=replicas,
        date_field=date,
        since=since,
        until=until,
    )
    index, page, tasks, contributors = await store.task_page(
        project_id, filters, sort=sort, order=order, limit=limit, offset=offset, around=around, contributors=TaskInclude.CONTRIBUTORS in includes
    )
    listing: dict[str, Any] = index.model_dump(mode="json")
    named: set[str] = set()
    if TaskInclude.DETAILS in includes:
        # Rule 5 (§5.2): once the project is publishing/published/archived, EVERY task transition is refused, so the
        # details must not hand the UI actions that can only 409.
        project = await store.get_project(project_id)
        project_frozen = project is not None and project.state in FROZEN_PROJECT_STATES
        details = [{**task.model_dump(mode="json"), "legal_events": [] if project_frozen else legal_task_events(task.state)} for task in tasks]
        found = {d["task_id"] for d in details}
        listing |= {
            "details": details,
            "missing": [tid for tid in page.task_ids if tid not in found],
            "matched": page.matched,
            "state_counts": {str(k): v for k, v in page.state_counts.items()},
            "offset": page.offset,
            "position": page.position,
            "datasets": page.datasets,
        }
        named |= principals_in(details)
    if contributors is not None:
        listing["contributors"] = [c.model_dump() for c in contributors]
        named |= {c.principal for c in contributors}
    if named:
        listing["people"] = await store.names_of(sorted(named)[:MAX_NAMED])
    return TaskListing.model_validate(listing)
