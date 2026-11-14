"""Annotation-project endpoints — slice S4, the create pin.

Create is a **create-on-parent** operation: at authorization time the child does not exist, so the
door is the organisation (`can_create_project` on `org:<tenant>`, in the workbench's own store), never the
child. Checking the child would be checking an object nobody holds a role on yet. The create then writes the new
project's place under its organisation and its creator's `owner` grant to the store.

The checker is injected rather than reached for, so the authorization contract is testable. Authorization decides *whether*; the state machine in `annotator.projects.machines` decides
*what* — they are deliberately separate.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from annotator.api.security import AuthorizerDep, CheckerDep, CurrentSubject, require_view, unseen_project
from annotator.api.v1.responses import LegalEvent, ProjectDetail, ProjectListing
from annotator.audit_actions import AnnotatorAuditAction
from annotator.projects.machines import legal_project_events
from annotator.projects.models import AnnotationProject, ProjectState
from annotator.projects.ontology import LabelOntology
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.fga import Authorizer, Grant, user_object
from service_kit.naming import CONTROL_ID_PATTERN


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["annotation-projects"])

#: The relation guarding creation, on the organisation: labeling is ordinary org work, and the creator becomes the new
#: project's owner.
CREATE_RELATION = "can_create_project"

#: The relation guarding the org's project LIST, on the organisation. An org member views every project below it (the
#: model's `viewer`), so the list's gate and each item's `can_view` agree by construction.
LIST_RELATION = "can_list_projects"

#: How many project-actor reads the landing keeps in flight at once. One actor id PER project, so
#: these genuinely parallelise rather than queueing on a single actor's turn lock. Matches the send
#: path's seed fan-out cap (`_ACTOR_FANOUT`) and the publish path's (`_COLLECT_FANOUT`); the bound
#: exists because the alternative is one sidecar channel per project in the tenant.
_LISTING_FANOUT: Final[int] = 16

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: The project permissions a project's read answers the caller holds: each one an action on its pages is gated on
#: (`service_kit/governed/auth/model.fga`). `can_view` is implied by the read itself.
READER_PERMISSIONS: Final[tuple[str, ...]] = ("can_administer", "can_manage", "can_publish", "can_review", "can_annotate", "can_claim", "can_send_items")


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: str = Field(min_length=1)
    #: The lakehouse warehouse publishes land in; empty means the warehouse named like the tenant.
    warehouse: str = Field(default="", pattern=rf"^(?:{CONTROL_ID_PATTERN})?$")
    slug: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    #: Annotator-facing labeling instructions (how to label) — distinct from `description` (what/why).
    instructions: str = Field(default="", max_length=20_000)
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0)
    #: Consensus v1 — create-only by design: `send` derives the replica count from it and the
    #: claim guard enumerates siblings with it, so changing it mid-flight would orphan replicas.
    consensus_n: int = Field(default=1, ge=1, le=5)
    #: The whole task definition — taxonomy, per-class tools, attributes, relations. Validated by
    #: its own model (including the cross-checks that the old `label_schema` + `template` pair had
    #: nothing to run); absent means an unconstrained project, which is the pre-ontology behaviour.
    ontology: LabelOntology = Field(default_factory=LabelOntology)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_annotation_project(payload: CreateProjectRequest, checker: CheckerDep, authz: AuthorizerDep, subject: CurrentSubject) -> AnnotationProject:
    """Create a project in `draft`. 403 when the caller may not create one in the target organisation.

    `subject` is the **verified** OIDC principal (`annotator.api.security`), not a header the caller
    supplied — every entity below is keyed on who owns or claims it, so a spoofable identity here
    would be a cross-user leak rather than a cosmetic issue.

    Fails CLOSED: any falsy check result denies. A project is born in `draft` — `open` is a separate,
    `can_manage`-gated transition, so creating one never implies it is ready to label.
    """
    parent = f"org:{payload.tenant}"
    allowed = await checker(user=subject, relation=CREATE_RELATION, obj=parent)
    if not allowed:
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_CREATE, AuditOutcome.FAILURE, subject=subject, resource=parent, relation=CREATE_RELATION)
        raise ForbiddenError(f"{subject} lacks {CREATE_RELATION} on {parent}")

    now = datetime.now(UTC)
    project = AnnotationProject(
        tenant=payload.tenant,
        warehouse=payload.warehouse,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        instructions=payload.instructions,
        review_required=payload.review_required,
        lease_seconds=payload.lease_seconds,
        consensus_n=payload.consensus_n,
        ontology=payload.ontology,
        state=ProjectState.DRAFT,
        created_at=now,
        updated_at=now,
        created_by=subject,
    )
    # ITS GRANTS FIRST: the project's place under its organisation and its creator's `owner`, so a project never
    # exists that nobody may manage. A grant on a project with no document yet answers nothing but a 404, which is why
    # these lead and the compensation below removes them.
    grants = (
        [
            Grant(user=project.org_object, relation="org", object=project.authz_object),
            Grant(user=user_object(subject), relation="owner", object=project.authz_object),
        ]
        if authz is not None
        else []
    )
    if authz is not None:
        await authz.write(grants)
    try:
        stored = await _create_actor(project.project_id).create(project.model_dump(mode="json"))
    except Exception:
        await _undo_grants(authz, grants)
        raise

    # THE SECOND WRITE, COMPENSATED. `project_id` is minted per attempt (`default_factory=new_id`), so a retry
    # creates a SECOND project and ORPHANS the first as a document nothing lists. So a failure past this point
    # undoes what it wrote and then fails. The compensation is idempotent and `discard` refuses anything but an
    # empty draft, which is what keeps this a create saga rather than a delete door (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" ANN-10).
    try:
        # REGISTER it in the tenant index — the actor `GET /projects` lists through. Synchronous and
        # NOT best-effort: an unregistered project is invisible to the landing forever.
        await _tenant_actor(payload.tenant).register({"project_id": project.project_id})
    except Exception:
        await _undo_create(project.project_id)
        await _undo_grants(authz, grants)
        # `reason`, not `relation`: the create was AUTHORIZED — a later write in the sequence
        # failed. Stamping the door here would read as a denial that never happened.
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_CREATE, AuditOutcome.FAILURE, subject=subject, resource=project.authz_object, reason="compensated")
        raise

    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_CREATE, AuditOutcome.SUCCESS, subject=subject, resource=project.authz_object)
    return AnnotationProject.model_validate(stored)


async def _undo_grants(authz: Authorizer | None, grants: list[Grant]) -> None:
    """Remove the create's grants, best-effort and logged, as `_undo_create` removes its document."""
    if authz is None or not grants:
        return
    try:
        await authz.delete(grants)
    except Exception:
        logger.exception("could not remove the grants of a failed create on %s", grants[0].object)


async def _undo_create(project_id: str) -> None:
    """Roll the create's document back, leaving nothing behind.

    Best-effort and logged: this runs because something already failed, and the ORIGINAL failure is the one the
    caller must see — a compensation that raises would replace the real reason with a second-order one. What a
    failed compensation leaves is exactly the orphan that existed before, now with a log line naming it.
    """
    try:
        await _create_actor(project_id).discard({})
    except Exception:
        logger.exception("could not discard project %s after a failed create — its document is orphaned in the actor state store", project_id)


class ListedFor(StrEnum):
    """An action a listing can be narrowed to: only the projects the caller may take it on."""

    SEND_ITEMS = "can_send_items"


@router.get("")
async def list_projects(
    tenant: Annotated[str, Query(min_length=1)],
    checker: CheckerDep,
    authz: AuthorizerDep,
    subject: CurrentSubject,
    can: ListedFor | None = None,
) -> ProjectListing:
    """The tenant's projects — A1's landing read.

    Gated on tenant MEMBERSHIP, and a refusal is a 403, never an empty 200: "you may not look"
    and "there is nothing here" are different answers, and collapsing them hides the former.
    Fans out to each project's own actor for the document, with concurrency bounded by
    `_LISTING_FANOUT`; an id whose actor holds no state (a lost partition) is skipped rather than
    taking the whole landing down.

    This is the estate's hottest actor fan-out — one call per page load — and each id addresses a
    DIFFERENT actor, so the reads are independent. Awaited in series the landing's wall clock is one
    sidecar round-trip per project in the tenant.
    """
    parent = f"org:{tenant}"
    if not await checker(user=subject, relation=LIST_RELATION, obj=parent):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_LIST, AuditOutcome.FAILURE, subject=subject, resource=parent, relation=LIST_RELATION)
        raise ForbiddenError(f"{subject} lacks {LIST_RELATION} on {parent}")

    listing = await _tenant_actor(tenant).list_projects()
    ids = [str(project_id) for project_id in listing["project_ids"]]
    if can is not None and authz is not None:
        # `?can=` narrows the list to the projects the caller holds that relation on, asked of the store in one call, so
        # a page offers a project only to someone its door would let act on it.
        held = set(await authz.list_objects(user=subject, relation=can.value, object_type="project"))
        ids = [project_id for project_id in ids if f"project:{project_id}" in held]
    gate = asyncio.Semaphore(_LISTING_FANOUT)

    async def _document(project_id: str) -> dict[str, Any] | None:
        async with gate:
            return await _create_actor(project_id).get()

    # `gather` preserves INPUT order, so the landing renders in the tenant index's order. It also
    # propagates the first failure rather than collecting it, which is deliberate: a listing that
    # silently omits a project the caller may see is indistinguishable from one that was deleted, so
    # an unreachable actor must be an error and not a shorter page.
    docs = await asyncio.gather(*(_document(project_id) for project_id in ids))
    projects = [doc for doc in docs if doc is not None]
    return ProjectListing(projects=[AnnotationProject.model_validate(doc) for doc in projects], total=len(projects))


@router.get("/{project_id}")
async def get_project(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject) -> ProjectDetail:
    """One project plus its LEGAL EVENTS — the read A1's detail page renders.

    `legal_events` comes from `machines.legal_project_events`, i.e. from the transition tables
    themselves: the UI renders what the backend supplies and never hardcodes a second copy of the
    machine that drifts. Each event carries the permission that gates it, so the UI can explain a
    disabled action — while the ACTUAL gate stays server-side on the event POST.
    """
    await require_view(checker, subject, f"project:{project_id}", unseen_project(project_id))
    doc = await _create_actor(project_id).get()
    if doc is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    project = AnnotationProject.model_validate(doc)
    held = await asyncio.gather(*(checker(user=subject, relation=relation, obj=f"project:{project_id}") for relation in READER_PERMISSIONS))
    return ProjectDetail(
        project=project,
        legal_events=[LegalEvent.model_validate(e) for e in legal_project_events(project.state)],
        permissions=sorted(relation for relation, allowed in zip(READER_PERMISSIONS, held, strict=True) if allowed),
    )


class UpdateOntologyRequest(BaseModel):
    """The whole ontology, replaced. Not a partial merge — see the route."""

    ontology: LabelOntology


#: Editing the task definition is a MANAGE act, not a labeling one: it changes what every future
#: item promises downstream. Same relation the `open`/`freeze`/`publish` transitions carry.
MANAGE_RELATION = "can_manage"

#: The ontology may be edited only while the project can still receive work. Past `frozen` the
#: answer set is closed and a publish is being prepared against it, so an edit could only either be
#: ignored (every remaining item already captured its copy) or misleading — the run facet would
#: report a taxonomy that no task was ever judged against.
ONTOLOGY_EDITABLE_STATES = frozenset({ProjectState.DRAFT, ProjectState.LABELING})


@router.patch("/{project_id}/ontology")
async def update_project_ontology(
    project_id: ProjectId,
    payload: UpdateOntologyRequest,
    checker: CheckerDep,
    subject: CurrentSubject,
) -> AnnotationProject:
    """Replace a project's ontology. 403 without `can_manage`, 409 once the project is past labeling.

    WHOLE-DOCUMENT replace, deliberately. A partial merge would have to answer "what does an absent
    `classes` mean" — cleared, or unchanged? — and the two readings differ by an entire taxonomy.
    The ontology is also cross-checked as a UNIT (relations must reference declared classes), so a
    merge would have to re-validate the merged result anyway; taking the whole document makes the
    thing validated and the thing stored the same object.

    Items already sent are UNAFFECTED: each captured its own copy at send, so work in review is
    judged by the contract it was issued under. That is the point of the capture, and it is what
    makes editing safe enough to allow during `labeling` at all.
    """
    obj = f"project:{project_id}"
    if not await checker(user=subject, relation=MANAGE_RELATION, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_ONTOLOGY, AuditOutcome.FAILURE, subject=subject, resource=project_id, relation=MANAGE_RELATION)
        raise ForbiddenError(f"{subject} lacks {MANAGE_RELATION} on {obj}")

    actor = _create_actor(project_id)
    doc = await actor.get()
    if doc is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    state = AnnotationProject.model_validate(doc).state
    if state not in ONTOLOGY_EDITABLE_STATES:
        raise ConflictError(f"project {project_id} is {state.value} — the ontology is editable only in {sorted(s.value for s in ONTOLOGY_EDITABLE_STATES)}")

    stored = await actor.set_ontology({"ontology": payload.ontology.model_dump(mode="json")})
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_ONTOLOGY, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=MANAGE_RELATION)
    return AnnotationProject.model_validate(stored)


def _create_actor(project_id: str) -> Any:
    """Lazy, for the same reason every other proxy here is: the proxy opens a sidecar channel.
    `typed_proxy` translates Python names to the interface's wire names — a raw `ActorProxy`
    dispatches only the wire names and raises `AttributeError` on these very calls in-cluster."""
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415 - deliberate
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415

    return typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)


def _tenant_actor(tenant: str) -> Any:
    """The tenant-projects index actor — same lazy typed-proxy pattern."""
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - deliberate
    from annotator.projects.tenant_actor import TenantProjectsActorInterface  # noqa: PLC0415

    return typed_proxy("TenantProjectsActor", tenant, TenantProjectsActorInterface)
