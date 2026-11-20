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
from enum import StrEnum
from typing import Annotated, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from annotator.api.dependencies import StoreDep
from annotator.api.security import AuthorizerDep, CheckerDep, CurrentSubject, lacking, require_view, unseen_project
from annotator.api.v1.responses import LegalEvent, ProjectDetail, ProjectListing
from annotator.audit_actions import AnnotatorAuditAction
from annotator.people import RECORDS_PERSON
from annotator.projects.machines import legal_project_events
from annotator.projects.models import (
    DESCRIPTION_MAX,
    INSTRUCTIONS_MAX,
    LEASE_MAX_SECONDS,
    TITLE_MAX,
    AnnotationProject,
    ProjectSettings,
    ProjectState,
)
from annotator.projects.ontology import RequestOntology
from annotator.text import Text
from service_kit.exceptions import NotFoundError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.fga import Authorizer, Grant, user_object
from service_kit.naming import CONTROL_ID_PATTERN


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["annotation-projects"], dependencies=RECORDS_PERSON)

#: The relation guarding creation, on the organisation: labeling is ordinary org work, and the creator becomes the new
#: project's owner.
CREATE_RELATION = "can_create_project"

#: The relation guarding the org's project LIST, on the organisation. An org member views every project below it (the
#: model's `viewer`), so the list's gate and each item's `can_view` agree by construction.
LIST_RELATION = "can_list_projects"

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: The project permissions a project's read answers the caller holds: each one an action on its pages is gated on
#: (`service_kit/governed/auth/model.fga`). `can_view` is implied by the read itself.
READER_PERMISSIONS: Final[tuple[str, ...]] = ("can_administer", "can_manage", "can_publish", "can_review", "can_annotate", "can_claim", "can_send_items")


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: Annotated[Text, Field(min_length=1, max_length=256)]
    #: The lakehouse warehouse publishes land in; empty means the warehouse named like the tenant.
    warehouse: str = Field(default="", pattern=rf"^(?:{CONTROL_ID_PATTERN})?$")
    slug: Annotated[Text, Field(min_length=1, max_length=128)]
    title: Annotated[Text, Field(max_length=TITLE_MAX)] = ""
    description: Annotated[Text, Field(max_length=DESCRIPTION_MAX)] = ""
    #: Annotator-facing labeling instructions (how to label) — distinct from `description` (what/why).
    instructions: Annotated[Text, Field(max_length=INSTRUCTIONS_MAX)] = ""
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0, le=LEASE_MAX_SECONDS)
    #: Consensus v1 — create-only by design: `send` derives the replica count from it and the
    #: claim guard enumerates siblings with it, so changing it mid-flight would orphan replicas.
    consensus_n: int = Field(default=1, ge=1, le=5)
    #: The whole task definition — taxonomy, per-class tools, attributes, relations. Validated by
    #: its own model; absent means an unconstrained project.
    ontology: RequestOntology = Field(default_factory=RequestOntology)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_annotation_project(
    payload: CreateProjectRequest, checker: CheckerDep, authz: AuthorizerDep, subject: CurrentSubject, store: StoreDep
) -> AnnotationProject:
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
        raise lacking(subject, CREATE_RELATION, parent)

    # Its creation is stamped by the store, on the database's clock.
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
        ontology=payload.ontology.stored(),
        state=ProjectState.DRAFT,
        created_by=subject,
    )
    # ITS GRANTS FIRST: the project's place under its organisation and its creator's `owner`, so a project never
    # exists that nobody may manage. A grant on a project with no row answers nothing but a 404, which is why these
    # lead and a failed write of the row removes them.
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
        # One row, listed by its organisation from the moment it commits.
        stored = await store.create_project(project)
    except Exception:
        await _undo_grants(authz, grants)
        # `reason`, not `relation`: the create was AUTHORIZED, and a later write failed.
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_CREATE, AuditOutcome.FAILURE, subject=subject, resource=project.authz_object, reason="compensated")
        raise

    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_CREATE, AuditOutcome.SUCCESS, subject=subject, resource=project.authz_object)
    return stored


async def _undo_grants(authz: Authorizer | None, grants: list[Grant]) -> None:
    """Remove the create's grants, best-effort and logged: this runs because something already failed, and the
    ORIGINAL failure is the one the caller must see."""
    if authz is None or not grants:
        return
    try:
        await authz.delete(grants)
    except Exception:
        logger.exception("could not remove the grants of a failed create on %s", grants[0].object)


class ListedFor(StrEnum):
    """An action a listing can be narrowed to: only the projects the caller may take it on."""

    SEND_ITEMS = "can_send_items"


@router.get("")
async def list_projects(
    tenant: Annotated[Text, Query(min_length=1, max_length=256)],
    checker: CheckerDep,
    authz: AuthorizerDep,
    subject: CurrentSubject,
    store: StoreDep,
    can: ListedFor | None = None,
) -> ProjectListing:
    """The tenant's projects — A1's landing read, oldest first.

    Gated on tenant MEMBERSHIP, and a refusal is a 403, never an empty 200: "you may not look"
    and "there is nothing here" are different answers, and collapsing them hides the former.
    """
    parent = f"org:{tenant}"
    if not await checker(user=subject, relation=LIST_RELATION, obj=parent):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_LIST, AuditOutcome.FAILURE, subject=subject, resource=parent, relation=LIST_RELATION)
        raise lacking(subject, LIST_RELATION, parent)

    projects = await store.list_projects(tenant)
    if can is not None and authz is not None:
        # `?can=` narrows the list to the projects the caller holds that relation on, asked of the store in one call, so
        # a page offers a project only to someone its door would let act on it.
        held = set(await authz.list_objects(user=subject, relation=can.value, object_type="project"))
        projects = [project for project in projects if project.authz_object in held]
    return ProjectListing(projects=projects, total=len(projects))


@router.get("/{project_id}")
async def get_project(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject, store: StoreDep) -> ProjectDetail:
    """One project plus its LEGAL EVENTS — the read A1's detail page renders.

    `legal_events` comes from `machines.legal_project_events`, i.e. from the transition tables
    themselves: the UI renders what the backend supplies and never hardcodes a second copy of the
    machine that drifts. Each event carries the permission that gates it, so the UI can explain a
    disabled action — while the ACTUAL gate stays server-side on the event POST.
    """
    await require_view(checker, subject, f"project:{project_id}", unseen_project(project_id))
    project = await store.get_project(project_id)
    if project is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    held = await asyncio.gather(*(checker(user=subject, relation=relation, obj=f"project:{project_id}") for relation in READER_PERMISSIONS))
    return ProjectDetail(
        project=project,
        legal_events=[LegalEvent.model_validate(e) for e in legal_project_events(project.state)],
        permissions=sorted(relation for relation, allowed in zip(READER_PERMISSIONS, held, strict=True) if allowed),
    )


class UpdateOntologyRequest(BaseModel):
    """The whole ontology, replaced. Not a partial merge — see the route."""

    ontology: RequestOntology


#: Editing the task definition is a MANAGE act, not a labeling one: it changes what every future
#: item promises downstream. Same relation the `open`/`freeze`/`publish` transitions carry.
MANAGE_RELATION = "can_manage"


@router.patch("/{project_id}/ontology")
async def update_project_ontology(
    project_id: ProjectId,
    payload: UpdateOntologyRequest,
    checker: CheckerDep,
    subject: CurrentSubject,
    store: StoreDep,
) -> AnnotationProject:
    """Replace a project's ontology. 403 without `can_manage`, 409 once the project is past labeling.

    WHOLE-DOCUMENT replace, deliberately: a partial merge would have to answer what an absent `classes` means, and the
    ontology is cross-checked as a unit, so the thing validated and the thing stored are the same object.

    Items already sent are UNAFFECTED: each captured its own copy at send, so work in review is
    judged by the contract it was issued under.
    """
    obj = f"project:{project_id}"
    if not await checker(user=subject, relation=MANAGE_RELATION, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_ONTOLOGY, AuditOutcome.FAILURE, subject=subject, resource=project_id, relation=MANAGE_RELATION)
        raise lacking(subject, MANAGE_RELATION, obj)
    if await store.get_project(project_id) is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    stored = await store.set_ontology(project_id, payload.ontology.stored())
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_ONTOLOGY, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=MANAGE_RELATION)
    return stored


@router.patch("/{project_id}")
async def update_project_settings(
    project_id: ProjectId,
    payload: ProjectSettings,
    checker: CheckerDep,
    subject: CurrentSubject,
    store: StoreDep,
) -> AnnotationProject:
    """Change a project's title, description, instructions, review and claim settings, or its consensus.

    403 without `can_manage`; 404 for no such project; 409 when the project's state, or the items it already holds, keep a
    setting fixed (`AnnotatorStore.update_settings` names which). Only the fields the body names change.
    """
    obj = f"project:{project_id}"
    if not await checker(user=subject, relation=MANAGE_RELATION, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_SETTINGS, AuditOutcome.FAILURE, subject=subject, resource=project_id, relation=MANAGE_RELATION)
        raise lacking(subject, MANAGE_RELATION, obj)
    if await store.get_project(project_id) is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    stored = await store.update_settings(project_id, payload)
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_UPDATE_SETTINGS, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=MANAGE_RELATION)
    return stored
