"""Task endpoints — the CVAT/Label-Studio loop: send, assign, claim, save, submit, review.

Each route authorizes the ONE `can_*` its event requires, on the **verified** subject, then hands the transition to the
state store. The permission is not hardcoded here: `annotator.projects.machines` carries it on the edge
(`TASK_EDGES[(state, event)] -> (target, permission)`), so the op→privilege map has a single home and a route cannot
drift from the model.

Two rules the machine cannot express, because they need the task's own rows:

- **Self-review is forbidden.** A reviewer may not accept their own submission (§5.2).
- **The lease holder is the only one who may save or submit.** `can_annotate` says you may annotate
  in this project; holding the claim says you may annotate THIS task right now.

Both are answered here against the task as this request read it, which is what lets a refusal be a 403 with its
reason, and answered again by the store inside the transaction holding the task's row lock
(`annotator.state.store`), which is what decides: between the read here and the lock the lease can move and the
reviewer can become the author. `can_manage`, which the store cannot compute, is resolved here from the verified
subject and the checker's own answer, and stated to it like the subject itself, never read off the request body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from annotator.api.dependencies import StoreDep, get_store
from annotator.api.security import CAN_REVIEW, Checker, CheckerDep, CurrentSubject, SettingsDep, lacking, require_view, unseen_task
from annotator.api.v1.responses import DraftImport, LegalEvent, TaskDetail
from annotator.audit_actions import AnnotatorAuditAction
from annotator.people import RECORDS_PERSON
from annotator.projects.imports import shapes_from_ipc
from annotator.projects.machines import (
    EDIT_PERMISSION,
    FROZEN_PROJECT_STATES,
    TASK_EDGES,
    CallerFacts,
    Refusal,
    caller_events,
    edit_violation,
    event_refusal,
    identity_violation,
)
from annotator.projects.models import AnnotationProject, Draft, Link, Shape, Task, TaskState
from annotator.state.store import AnnotatorStore
from annotator.state.task_query import principals_in
from annotator.text import Id, Text
from workbench.exceptions import ConflictError, DomainError, ForbiddenError, NotFoundError, ValidationError
from workbench.governed.audit import AuditOutcome, audit
from workbench.governed.fga import Grant
from workbench.governed.principal import is_principal


router = APIRouter(prefix="/tasks", tags=["annotation-tasks"], dependencies=[Depends(get_store), *RECORDS_PERSON])

#: Edges whose `TASK_EDGES` permission is `None` — caused by the system (a lease running out), never by
#: a principal. They are REFUSED on the HTTP surface rather than waved through: "no permission
#: required" is a statement about who causes the edge, and treating it as "no permission
#: checked" would let anyone POST `lease_expired` to strip another annotator's claim.
SYSTEM_ONLY_EVENTS: Final[frozenset[str]] = frozenset(e for (_s, e), (_t, p) in TASK_EDGES.items() if p is None)

TaskId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class FireRequest(BaseModel):
    """One event against a task. `event` must be an edge in `TASK_EDGES`.

    Deliberately narrow. It carries NO project/tenant — the authorization object is read from the
    task's own record, so a caller cannot name the object its permission is checked against — and no
    `review_required`, which is captured on the task at send time from the project; a caller able to
    pass it would submit with review waived and self-accept.
    """

    event: Annotated[Text, Field(min_length=1, max_length=64)]
    #: Optional override; absent means "the project's lease", captured on the task at send. A
    #: hardcoded default HERE would silently outvote the project's configuration on every claim.
    lease_seconds: int | None = Field(default=None, gt=0, le=86_400)
    #: `assign` only. The user a MANAGER is assigning the task to — this is what makes the edge a
    #: distribution mechanism rather than a self-claim. Safe as a request field because it names the
    #: RECIPIENT, not the caller: the caller's own authority is still `can_manage`, checked against
    #: the task's project.
    assignee: Annotated[Text, Field(max_length=128)] | None = None
    #: `request_changes` only — the reviewer's reason, appended as a `ReviewNote`.
    message: Annotated[Text, Field(max_length=4000)] = ""
    shape_ids: list[Id] = Field(default_factory=list, max_length=500)
    #: A review decision on an item a judge flagged: whether the reviewer found the flag right. Absent, the decision
    #: says it (accepting the work as it is disagrees; fixing it or sending it back agrees). 409 on an item that
    #: carries no flag, and on an event that is no review decision.
    judge_agreed: bool | None = None


async def _authorize(checker: Checker, subject: str, permission: str, task: Task, what: AnnotatorAuditAction, **context: str) -> None:
    """Check one relation on the TASK, fail closed, and audit either way.

    The object is `task:<task_id>`, whose permissions the model takes from its project's roles. The task's project
    rides the check as a contextual edge (`task#project`) rather than a stored tuple per task: it comes from the TASK's
    own record, never from the request, so a caller cannot choose which project its permission is evaluated against.
    """
    obj = f"task:{task.task_id}"
    edge = Grant(user=f"project:{task.project_id}", relation="project", object=obj)
    if not await checker(user=subject, relation=permission, obj=obj, contextual=[edge]):
        audit(what, AuditOutcome.FAILURE, subject=subject, resource=obj, relation=permission, **context)
        raise lacking(subject, permission, obj)


async def viewed_task(store: AnnotatorStore, task_id: str, checker: Checker, subject: str) -> Task:
    """The task, once the caller may view it: a task that does not exist and one they may not view answer the same
    refusal (`unseen_task`), before any state or relation is named, since a task id is guessable (`{group}-r{k}`)."""
    task = await store.get_task(task_id)
    if task is None:
        raise NotFoundError(unseen_task(task_id))
    edge = Grant(user=f"project:{task.project_id}", relation="project", object=f"task:{task_id}")
    await require_view(checker, subject, f"task:{task_id}", unseen_task(task_id), contextual=[edge])
    return task


def reads_work(task: Task, subject: str, *, may_review: bool) -> bool:
    """Whether ``subject``, who may view the task, may read its work (its draft, its rows of its unit).

    A consensus replica's work is its own annotator's until a reviewer compares it: the replicas of an item are made to
    be independent, so a fellow annotator of the group, who may view the task as everyone on the project may, is refused
    it. Its holder and its submitter read it, and so does whoever may review the task (`can_review`: a reviewer, a
    manager or the owner, the roles the model chains above it), who adjudicates between replicas. Any other task's work
    is read by whoever may view it. The one rule the work doors (`require_work_readable`) and a listing's work read by."""
    return task.replica_of is None or subject in (task.assignee, task.submitted_by) or may_review


async def require_work_readable(task: Task, checker: Checker, subject: str) -> None:
    """The caller may read this viewed task's work, or 403 (`reads_work`); `can_review` is asked only when it decides."""
    if reads_work(task, subject, may_review=False):
        return
    edge = Grant(user=f"project:{task.project_id}", relation="project", object=f"task:{task.task_id}")
    if not await checker(user=subject, relation=CAN_REVIEW, obj=f"task:{task.task_id}", contextual=[edge]):
        audit(AnnotatorAuditAction.ANNOTATOR_AUTHORIZE, AuditOutcome.FAILURE, subject=subject, resource=f"task:{task.task_id}", relation=CAN_REVIEW)
        raise ForbiddenError(
            "you may not read this replica's work: only its annotator, or someone who may review it",
            extensions={"principal": subject, "task": task.task_id},
        )


async def _refuse_if_frozen(store: AnnotatorStore, task: Task, event: str) -> None:
    """§5.2 rule 5 — nothing escapes a published project — answered before the transition so the 409 names it; the
    store answers it again on the project row its transaction holds."""
    project = await store.get_project(task.project_id)
    if project is not None and project.state in FROZEN_PROJECT_STATES:
        raise ConflictError(
            f"this project is {project.state} — {event} is rejected; provenance is frozen with the published artifact",
            extensions={"project": task.project_id},
        )


async def require_editable(store: AnnotatorStore, task: Task, checker: Checker, subject: str, what: AnnotatorAuditAction) -> None:
    """The caller may edit this viewed task's shapes now, or the refusal that says why.

    The one rule every write of a task's shapes answers to: the canvas's saves of its draft
    (`annotator.annotations.access`) and an import into it. Asked in this order, so a refusal names no more than its step:
    - 409 when the task's state takes no edits (`EDIT_PERMISSION`), or its project is frozen;
    - 403 when the caller lacks the state's relation on the task, or is not who may edit it now (`edit_violation`).
    """
    permission = EDIT_PERMISSION.get(task.state)
    if permission is None:
        raise ConflictError(f"this item is {task.state} — its shapes take edits only while it is claimed or in review", extensions={"task": task.task_id})
    await _refuse_if_frozen(store, task, "save_draft")
    await _authorize(checker, subject, permission, task, what)
    violation = edit_violation(state=task.state, subject=subject, assignee=task.assignee, submitted_by=task.submitted_by)
    if violation is not None:
        audit(what, AuditOutcome.FAILURE, subject=subject, resource=f"task:{task.task_id}", reason="identity_bound")
        raise ForbiddenError(violation, extensions=_identity_fields(task))


@router.post("/{task_id}/events", status_code=status.HTTP_200_OK)
async def fire_task_event(
    task_id: TaskId, payload: FireRequest, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep, store: StoreDep
) -> TaskDetail:
    """Drive one task transition.

    The permission comes from the transition table, so adding an edge to `TASK_EDGES` automatically
    gates it — there is no per-route ladder here to forget to update.
    """
    # VIEW FIRST: the relation an event needs depends on the task's state, so nothing below is said to someone who may
    # not view the task.
    current = await viewed_task(store, task_id, checker, subject)
    if payload.event in SYSTEM_ONLY_EVENTS:
        raise ForbiddenError(f"{payload.event} is caused by the system, not by a principal, and cannot be fired over HTTP")

    # THE ONE RULE (`machines.event_refusal`), read from the same facts every listing offers by (`caller_facts`), so the
    # door refuses exactly what no page offered. The store judges its identity and replica halves again under the lock,
    # and judges an `assign`'s RECIPIENT, whom no offer can know.
    [facts] = await caller_facts(store, checker, subject, await store.get_project(current.project_id), [current])
    refusal = event_refusal(payload.event, current, facts)
    if refusal is not None:
        raise _refused(refusal, current, facts, payload.event)
    # An assignee is the subject the assigned person's own token will prove: with authentication on that
    # is a principal, so a bare name, which no token proves, is refused rather than stored for nobody to claim.
    if settings.oidc_enabled and payload.assignee is not None and not is_principal(payload.assignee):
        raise ValidationError(f"assignee {payload.assignee!r} is not a principal: '<idp-id>~<subject>'")
    # §5.2 — `release` is the documented escape hatch for a task pinned to someone unavailable, so a manager may take it:
    # the store re-checks the rule against the holder of the moment, and needs the answer then.
    subject_can_manage = "can_manage" in facts.held

    # `actor` is always the VERIFIED caller; `assignee` is the recipient a manager names on `assign`. Rows that moved
    # between this read and the transaction are an IllegalTransition, a 409.
    updated = await store.fire_task(
        task_id,
        event=payload.event,
        actor=subject,
        subject_can_manage=subject_can_manage,
        lease_seconds=payload.lease_seconds,
        assignee=payload.assignee,
        message=payload.message,
        shape_ids=payload.shape_ids,
        judge_agreed=payload.judge_agreed,
    )

    audit(AnnotatorAuditAction.TASK_TRANSITION, AuditOutcome.SUCCESS, event=payload.event, subject=subject, resource=task_id)
    return await with_legal_events(store, updated, checker, subject)


def _identity_fields(task: Task) -> dict[str, str]:
    """The machine fields of an identity refusal: the item, and the principal holding it, kept out of the prose."""
    return {"task": task.task_id} | ({"holder": task.assignee} if task.assignee else {})


#: The project permissions a task's edges are gated on (`TASK_EDGES`): what `events_for` asks the checker for.
_EDGE_PERMISSIONS: Final[tuple[str, ...]] = tuple(sorted({permission for _target, permission in TASK_EDGES.values() if permission is not None}))


async def caller_facts(store: AnnotatorStore, checker: Checker, subject: str, project: AnnotationProject | None, tasks: Sequence[Task]) -> list[CallerFacts]:
    """What the one rule reads of ``subject`` for each of ``tasks`` (one project's): the project permissions they hold
    (asked once: a task's relations are its project's, `model.fga`), the project's state when it is past labeling, and
    whether they hold, held or worked another replica of each task's group (one statement for the whole page)."""
    if project is None:
        return [CallerFacts(subject=subject, held=frozenset()) for _ in tasks]
    obj = f"project:{project.project_id}"
    answers = await asyncio.gather(*(checker(user=subject, relation=permission, obj=obj) for permission in _EDGE_PERMISSIONS))
    held = frozenset(permission for permission, allowed in zip(_EDGE_PERMISSIONS, answers, strict=True) if allowed)
    frozen = project.state if project.state in FROZEN_PROJECT_STATES else None
    worked = await store.worked_replicas(project.project_id, subject) if any(t.replica_of for t in tasks) else {}
    return [
        CallerFacts(
            subject=subject, held=held, frozen=frozen, other_replica=min(worked.get(t.replica_of, set()) - {t.task_id}, default=None) if t.replica_of else None
        )
        for t in tasks
    ]


def _refused(refusal: Refusal, task: Task, facts: CallerFacts, event: str) -> DomainError:
    """A refusal of the one rule as the door answers it: 403 for who the caller is (their role, an identity rule), 409
    for where the item is (no such edge, a frozen project, another replica already theirs); audited when it is about who
    they are."""
    obj, subject = f"task:{task.task_id}", facts.subject
    match refusal.kind:
        case "replica":
            return ConflictError(refusal.reason, extensions={"principal": subject, "replica": facts.other_replica or "", "group": task.replica_of or ""})
        case "permission":
            permission = refusal.permission or ""
            audit(AnnotatorAuditAction.TASK_TRANSITION, AuditOutcome.FAILURE, subject=subject, resource=obj, relation=permission, event=event)
            return lacking(subject, permission, obj)
        case "identity":
            audit(AnnotatorAuditAction.TASK_TRANSITION, AuditOutcome.FAILURE, event=event, subject=subject, resource=task.task_id, reason="identity_bound")
            return ForbiddenError(refusal.reason, extensions=_identity_fields(task))
        case "system":
            return ForbiddenError(refusal.reason)
        case "frozen":
            return ConflictError(refusal.reason, extensions={"project": task.project_id})
        case "illegal":
            return ConflictError(refusal.reason)


async def events_for(store: AnnotatorStore, checker: Checker, subject: str, project: AnnotationProject | None, tasks: Sequence[Task]) -> list[list[LegalEvent]]:
    """What ``subject`` may fire on each of ``tasks`` (one project's) now, in order: the edges out of each task's state
    the one rule refuses them nothing (`machines.caller_events`, the rule the door refuses by), read from the facts of
    `caller_facts`. Every surface offers exactly these. An `assign` is offered to who may distribute work; its RECIPIENT
    is judged when named, and refused 409, per item, when they hold, held or worked another replica of it."""
    facts = await caller_facts(store, checker, subject, project, tasks)
    return [[LegalEvent.model_validate(e) for e in caller_events(task, mine)] for task, mine in zip(tasks, facts, strict=True)]


async def with_legal_events(store: AnnotatorStore, task: Task, checker: Checker, subject: str) -> TaskDetail:
    """The task with what the caller may fire on it now (`events_for`), as the queue's details carry them."""
    [events] = await events_for(store, checker, subject, await store.get_project(task.project_id), [task])
    return TaskDetail.model_validate(
        {**task.model_dump(), "legal_events": events, "people": await store.names_of(principals_in([task.model_dump(mode="json")]))}
    )


@router.get("/{task_id}")
async def get_task(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject, store: StoreDep) -> TaskDetail:
    """Read one task, with what the caller may fire on it now. The task is fetched FIRST to learn which project to
    authorize against, then the check runs before anything is returned — the fetch is not the disclosure, the return is."""
    return await with_legal_events(store, await viewed_task(store, task_id, checker, subject), checker, subject)


@router.post("/{task_id}/import", status_code=status.HTTP_200_OK)
async def import_annotations(
    task_id: TaskId, request: Request, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep, store: StoreDep
) -> DraftImport:
    """Import annotations made elsewhere into this task's draft, as Arrow IPC.

    ONE format — the canonical annotations schema — because the workbench already has one and three
    parsers inside the service would be three things that rot. Converting COCO or YOLO into it is a
    `scripts/` concern. See `annotator.projects.imports`.

    **APPENDS to the draft; it does not replace it.** An import onto a task somebody had already worked would
    otherwise destroy that work with no undo. Appending is never destructive, composes (importing twice gives two
    sets, visibly), and leaves duplicates removable on the canvas.

    The same state rules as a draft save, and for the same reason: importing is annotating. A task
    that is not CLAIMED by this caller cannot receive one.
    """
    current = await viewed_task(store, task_id, checker, subject)
    await _authorize(checker, subject, "can_annotate", current, AnnotatorAuditAction.TASK_IMPORT)
    await _refuse_if_frozen(store, current, "import")
    if current.state is not TaskState.CLAIMED:
        raise ConflictError(f"this item is {current.state} — annotations can only be imported into a claimed one", extensions={"task": task_id})
    # An import IS a draft write, so it is refused by the same rule under the same event name.
    violation = identity_violation(event="save_draft", subject=subject, assignee=current.assignee, submitted_by=current.submitted_by)
    if violation is not None:
        raise ForbiddenError(violation, extensions=_identity_fields(current))

    existing = await store.get_draft(task_id)
    taken = {s.shape_id for s in existing.shapes} if existing else set()
    # The ontology CAPTURED on the task, not one the client sends: the caller must not supply the rules it is judged by.
    ontology = current.ontology if current.ontology.constrains else None

    payload = await request.body()
    # OFF THE LOOP: the decode is synchronous CPU over a caller-sized body, and inline it would freeze every other
    # request this process serves, its own probes included.
    shapes, links = await run_in_threadpool(shapes_from_ipc, payload, max_bytes=settings.max_body_bytes, ontology=ontology, taken_ids=taken)

    def append(current: Draft | None, _now: datetime) -> tuple[list[Shape], list[Link]]:
        return [*(current.shapes if current else []), *shapes], [*(current.links if current else []), *links]

    draft = await store.change_draft(
        task_id,
        author=subject,
        change=append,
        # The revision the ids were checked against, so a save that lands in between is a 409 rather than a clash.
        base_revision=existing.revision if existing else 0,
        origin="import",
    )

    audit(AnnotatorAuditAction.TASK_IMPORT, AuditOutcome.SUCCESS, subject=subject, resource=task_id)
    return DraftImport(imported=len(shapes), links=len(links), draft=draft)


@router.get("/{task_id}/draft")
async def get_draft(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject, store: StoreDep) -> Draft | None:
    """The task's draft, or null when none is written yet: an answer, not a failure, so a client tells "start the first
    draft" from a read that failed without a refusal on the wire."""
    await require_work_readable(await viewed_task(store, task_id, checker, subject), checker, subject)
    return await store.get_draft(task_id)
