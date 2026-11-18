"""Who may do what on one annotation project — read, grant, revoke.

The roles are grants in the workbench's own OpenFGA store (`annotator.projects.roles`): ``owner`` implies ``manager``
implies ``reviewer`` implies ``annotator`` implies ``viewer``. A project's creator is its first owner; this is the
surface that adds everyone else, so a collaborative project needs no shared account. Every service reads the same
grants, so a role granted here is what flows answers on too.

Reads are `can_manage`-gated too. Who has access to a thing is itself sensitive — it names the people worth phishing —
and a viewer has no business enumerating them.

The owner and manager roles are the owner's to grant and revoke (`can_administer`); the others are a manager's
(`can_manage`). So a manager can neither make themselves owner nor remove the owner.

THE LAST OWNER OR MANAGER IS NEVER REVOKED, without a lock. A revoke reads the project's grants first and refuses
one that would leave no owner or manager, before anything is deleted: a read that fails is a 503 with every grant in
place. Two revokes racing past that read are caught after the delete: the revoke reads the grants again and writes its
grant back when none is left. The last revoke to read sees every delete made before it, so a project keeps one, and at
worst both revokes are refused; only a pod that dies between that delete and its read-back can leave a raced project
with none, which the first check makes reachable only by two administrators revoking each other at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Path, Query, Request, status
from pydantic import BaseModel, Field

from annotator.api.dependencies import StoreDep
from annotator.api.security import AuthorizerDep, CheckerDep, CurrentSubject, SettingsDep, lacking
from annotator.audit_actions import AnnotatorAuditAction
from annotator.core.config import AnnotatorSettings
from annotator.people import RECORDS_PERSON, names_for
from annotator.projects.roles import ADMINISTRATIVE, GRANTABLE, MemberGrant, Role
from annotator.text import Text
from service_kit.exceptions import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.deps import FgaChecker
from service_kit.governed.fga import Authorizer, Grant, subject_of, user_object
from service_kit.governed.principal import is_principal


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["members"], dependencies=RECORDS_PERSON)

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class MemberList(BaseModel):
    #: Each direct grant, as stored: its `user` is what a revoke sends back.
    members: list[MemberGrant]
    #: The roles this API will grant, so a UI does not hardcode a second copy of the ladder.
    grantable: list[str] = Field(default_factory=lambda: list(GRANTABLE))
    #: Each member's principal → the name they last wrote under, where one is kept (`annotator.people`).
    people: dict[str, str] = Field(default_factory=dict)


async def _listed(request: Request, members: list[MemberGrant]) -> MemberList:
    return MemberList(members=members, people=await names_for(request, (m.user for m in members)))


class GrantRequest(BaseModel):
    #: The person's principal key. A `user:`-prefixed id an earlier reader echoed back is accepted as the key it names.
    user: Annotated[Text, Field(min_length=1, max_length=256)]
    relation: Role


def _person(raw: str, settings: AnnotatorSettings) -> str:
    """The principal key a grant names; with authentication on it must be one a person's token can prove."""
    key = unquote(raw.removeprefix("user:"))
    if settings.oidc_enabled and not is_principal(key):
        raise ValidationError(f"{raw!r} is not a principal: '<idp-id>~<subject>'")
    return key


def _store(authz: Authorizer | None) -> Authorizer:
    """The members door writes grants, so it needs the store whatever the checker allows."""
    if authz is None:
        raise ServiceUnavailableError("project members are grants in the workbench's store, and authorization is off")
    return authz


async def _require(checker: FgaChecker, subject: str, project_id: str, action: AnnotatorAuditAction, relation: str = "can_manage") -> None:
    obj = f"project:{project_id}"
    if not await checker(user=subject, relation=relation, obj=obj):
        audit(action, AuditOutcome.FAILURE, subject=subject, resource=project_id, relation=relation)
        raise lacking(subject, relation, obj)


def _guarding(role: str) -> str:
    """The relation that grants or revokes ``role``: the owner's for the administrative roles, a manager's otherwise."""
    return "can_administer" if role in ADMINISTRATIVE else "can_manage"


def _keeps_an_administrator(members: list[MemberGrant], user: str, relation: str) -> bool:
    return any(m.relation in ADMINISTRATIVE and (m.user, m.relation) != (user, relation) for m in members)


async def _members(authz: Authorizer, project_id: str) -> list[MemberGrant]:
    """The project's direct grants to people, as stored."""
    held = await authz.grants_on(f"project:{project_id}")
    return sorted(
        (
            MemberGrant.model_validate({"user": subject_of(g.user), "relation": g.relation})
            for g in held
            if g.user.startswith("user:") and g.relation in GRANTABLE
        ),
        key=lambda m: (m.user, m.relation),
    )


@router.get("/{project_id}/members")
async def list_members(request: Request, project_id: ProjectId, checker: CheckerDep, authz: AuthorizerDep, subject: CurrentSubject) -> MemberList:
    """Who holds which role directly on this project.

    DIRECT grants only. An org admin who owns every project of the org is not listed, because they are not revocable
    here and showing an entry whose remove button cannot work is worse than not showing it.
    """
    await _require(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_LIST)
    return await _listed(request, await _members(_store(authz), project_id))


#: The most projects whose members a candidates read gathers (the one granted on first), and the most people it answers.
MAX_CANDIDATE_PROJECTS = 100
MAX_CANDIDATES = 500
#: How many objects OpenFGA's ListObjects answers at most (its server's default `listObjects-max-results`): an answer
#: this long may have been cut, and the candidates then say they may be incomplete.
LIST_OBJECTS_MAX = 1000
#: How long the read of the other projects' members may take, all of them at once; past it the candidates are the ones
#: read so far, said to be incomplete.
CANDIDATES_DEADLINE_S = 5.0


class Candidate(BaseModel):
    principal: str
    name: str


class Candidates(BaseModel):
    """People a granter may pick by name: see `member_candidates`."""

    people: list[Candidate]
    #: The set may be incomplete (more projects or people than it reads, OpenFGA's list cut short, or a read that ran
    #: out of time): someone not listed is granted by their typed principal.
    truncated: bool = False


@router.get("/{project_id}/members/candidates")
async def member_candidates(
    request: Request, project_id: ProjectId, checker: CheckerDep, authz: AuthorizerDep, subject: CurrentSubject, store: StoreDep
) -> Candidates:
    """The people a manager of this project may grant a role to by name: everyone holding a direct role on a project of
    this project's organisation that the CALLER manages, who has a name kept here (`annotator.people`: they signed in or
    wrote here). Each of those projects' members the caller may already list (`list_members` is `can_manage`-gated),
    with the same names beside them, so the set discloses nobody they could not already see. Someone outside it is
    granted by their typed principal.

    Bounded before any member is read: the project granted on always comes first, then the newest others the caller
    manages, at most `MAX_CANDIDATE_PROJECTS` in all (one indexed read of their ids), their members read at once within
    `CANDIDATES_DEADLINE_S`. ``truncated`` says when the set may be incomplete."""
    await _require(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_LIST)
    grants = _store(authz)
    project = await store.get_project(project_id)
    if project is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    managed = await grants.list_objects(user=subject, relation="can_manage", object_type="project")
    others = {obj.removeprefix("project:") for obj in managed} - {project_id}
    # One more than read, to know whether more were left out.
    newest = await store.newest_of_tenant(project.tenant, others, limit=MAX_CANDIDATE_PROJECTS)
    read = [project_id, *newest[: MAX_CANDIDATE_PROJECTS - 1]]
    truncated = len(managed) >= LIST_OBJECTS_MAX or len(newest) > MAX_CANDIDATE_PROJECTS - 1
    pending = [asyncio.ensure_future(_members(grants, pid)) for pid in read]
    done, late = await asyncio.wait(pending, timeout=CANDIDATES_DEADLINE_S)
    for task in late:
        task.cancel()
    principals = {member.user for task in done if task.exception() is None for member in task.result()}
    truncated = truncated or bool(late) or any(task.exception() is not None for task in done)
    names = await names_for(request, principals)
    people = sorted((Candidate(principal=p, name=n) for p, n in names.items()), key=lambda c: (c.name.casefold(), c.principal))
    return Candidates(people=people[:MAX_CANDIDATES], truncated=truncated or len(people) > MAX_CANDIDATES)


@router.put("/{project_id}/members", status_code=status.HTTP_200_OK)
async def grant_member(
    request: Request, project_id: ProjectId, payload: GrantRequest, checker: CheckerDep, authz: AuthorizerDep, subject: CurrentSubject, settings: SettingsDep
) -> MemberList:
    """Grant one role to one person. Idempotent — re-granting what someone already has is a no-op."""
    await _require(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_GRANT, _guarding(payload.relation))
    store = _store(authz)
    await store.write([Grant(user=user_object(_person(payload.user, settings)), relation=payload.relation, object=f"project:{project_id}")])
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_GRANT, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=payload.relation)
    return await _listed(request, await _members(store, project_id))


@router.delete("/{project_id}/members", status_code=status.HTTP_200_OK)
async def revoke_member(
    request: Request,
    project_id: ProjectId,
    user: Annotated[Text, Query(min_length=1, max_length=256)],
    relation: Role,
    checker: CheckerDep,
    authz: AuthorizerDep,
    subject: CurrentSubject,
    settings: SettingsDep,
) -> MemberList:
    """Revoke one role from one person; 409 for the project's last owner or manager (see the module docstring).

    `user` + `relation` ride as QUERY PARAMS, not a body: a DELETE body has no defined semantics (RFC 9110) and some
    intermediaries strip or refuse it. Revoking a role nobody holds is a no-op rather than a 404: the caller asked for a
    state and that state already holds.
    """
    await _require(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_REVOKE, _guarding(relation))
    store = _store(authz)
    person = _person(user, settings)

    async def refusal() -> ConflictError:
        # By the name the person is known here by, else as "this person": their principal is a machine field.
        who = (await names_for(request, [person])).get(person, "this person")
        return ConflictError(
            f"{who} holds the only remaining {relation} on this project — grant another owner or manager first",
            extensions={"principal": person, "relation": relation},
        )

    if relation in ADMINISTRATIVE and not _keeps_an_administrator(await _members(store, project_id), person, relation):
        raise await refusal()
    grant = Grant(user=user_object(person), relation=relation, object=f"project:{project_id}")
    await store.delete([grant])
    left = await _members(store, project_id)
    if relation in ADMINISTRATIVE and not any(m.relation in ADMINISTRATIVE for m in left):
        # Another revoke raced past the first check: restore this grant, so the project keeps an administrator.
        await store.write([grant])
        raise await refusal()
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_REVOKE, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=relation)
    return await _listed(request, left)
