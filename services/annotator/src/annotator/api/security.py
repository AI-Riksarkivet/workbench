"""Who the request is, and what the workbench's own store lets them do in the annotator.

Who: a person verified by the shared door (`service_kit.governed.deps`), a DPoP-bound token addressed to the workbench.

What: the workbench's own OpenFGA model (`service_kit/governed/auth/model.fga`): an organisation's admins and members,
a project's five concentric roles (owner > manager > reviewer > annotator > viewer), and the tasks and corpora that
inherit from a project. The annotator never asks the lakehouse what a person may do in the annotator.

Data: a corpus is a lakehouse's, or one of a source git marks annotatable (`client_kit.sources.annotatable`). Read as the
person (``identity: person``), the lakehouse decides, on a token the annotator exchanges or passes on for them
(`CallerDep`); read as the workbench (``identity: service``), and for every other source, the workbench's own store
decides first, `reader` on its source (`READS_DATASET`). Annotations
are written as the workbench's own identity once the annotator's own role check has passed, naming the person in the
commit (`annotator.annotations.commit`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import Depends, Query, Request

from annotator.annotations.store import ProjectAnnotations
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from client_kit.media.deps import StateDep
from client_kit.media.source_gate import named_in, require_read
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.deps import ANONYMOUS_SUBJECT, FgaChecker, make_auth_deps
from service_kit.governed.fga import Authorizer, Grant
from service_kit.governed.identity import Caller
from service_kit.governed.token_exchange import LakehouseCaller, LakehouseTokens


__all__ = [
    "ANONYMOUS_SUBJECT",
    "READS_DATASET",
    "AnnotationsDep",
    "AuthorizerDep",
    "CallerDep",
    "Checker",
    "CheckerDep",
    "CurrentSubject",
    "CurrentToken",
    "ProjectParam",
    "ServiceTokenDep",
    "SettingsDep",
    "authenticate",
    "current_subject",
    "get_checker",
    "require_view",
    "unseen_project",
    "unseen_task",
]

SettingsDep = Annotated[AnnotatorSettings, Depends(get_annotator_settings)]

_deps = make_auth_deps(SettingsDep)

# Module-level names so the routes' `Depends(...)` and the tests' `dependency_overrides` key on the same objects.
authenticate = _deps.authenticate
current_subject = _deps.current_subject
get_checker = _deps.get_checker
get_authorizer = _deps.get_authorizer

CurrentToken = Annotated[Caller | None, Depends(authenticate)]
CurrentSubject = Annotated[str, Depends(current_subject)]
#: The one question a route asks: may this caller do ``relation`` on ``obj``?
Checker = FgaChecker
CheckerDep = Annotated[Checker, Depends(get_checker)]
#: The store itself, for the doors that write grants (a project's creation, its members); ``None`` with it off.
AuthorizerDep = Annotated[Authorizer | None, Depends(get_authorizer)]
#: The person every corpus read is made as.
CallerDep = Annotated[LakehouseCaller, Depends(_deps.lakehouse_caller)]


def service_token(request: Request, settings: SettingsDep) -> LakehouseTokens | None:
    """The workbench's own tokens for the lakehouse, for an annotation write it makes as itself after its own check.

    ``None`` with authentication off (the directory catalog authenticates nobody); 503 when it is on and the annotator
    names no confidential client, since a write with no credential is refused by any lakehouse that authenticates.
    """
    if not settings.oidc_enabled:
        return None
    tokens: LakehouseTokens | None = getattr(request.app.state, "lakehouse_tokens", None)
    if tokens is None and settings.reads_lakehouse:
        raise ServiceUnavailableError("the annotator names no confidential client to write the lakehouse as itself")
    return tokens


ServiceTokenDep = Annotated[LakehouseTokens | None, Depends(service_token)]


#: The project a corpus read or an annotation write is made for, named by the request: its roles decide.
ProjectParam = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="The project this annotation work is for")]


def unseen_project(project_id: str) -> str:
    """The one refusal a person who may not view a project gets from any of its doors: the same words whether the project
    exists, which state it is in and what was asked, so no door is an oracle of either. It names no relation and no state."""
    return f"No project {project_id} that you can see"


def unseen_task(task_id: str) -> str:
    """`unseen_project`'s words for a task: a task that does not exist reads as one the caller may not view."""
    return f"No task {task_id} that you can see"


async def require_view(checker: FgaChecker, subject: str, obj: str, refusal: str, contextual: Iterable[Grant] = ()) -> None:
    """``can_view`` on ``obj``, asked before anything about the object is read: refused, the caller learns ``refusal`` and
    nothing else (404, as for an object that does not exist)."""
    from annotator.audit_actions import AnnotatorAuditAction
    from service_kit.exceptions import NotFoundError
    from service_kit.governed.audit import AuditOutcome, audit

    if not await checker(user=subject, relation="can_view", obj=obj, contextual=contextual):
        audit(AnnotatorAuditAction.ANNOTATOR_AUTHORIZE, AuditOutcome.FAILURE, subject=subject, resource=obj, relation="can_view")
        raise NotFoundError(refusal)


def project_gate(relation: str) -> Any:
    """A decorator dependency: the caller holds ``relation`` on the project the request names (``?project=``).

    Annotation reads and writes are a project's: its rows live in the project's own table (`annotator.annotations.store`),
    so the check is the project's role, never a corpus binding another project could make.
    """

    async def _gate(project: ProjectParam, subject: CurrentSubject, checker: CheckerDep) -> None:
        from annotator.audit_actions import AnnotatorAuditAction
        from service_kit.exceptions import ForbiddenError
        from service_kit.governed.audit import AuditOutcome, audit

        obj = f"project:{project}"
        if not await checker(user=subject, relation=relation, obj=obj):
            audit(AnnotatorAuditAction.ANNOTATOR_AUTHORIZE, AuditOutcome.FAILURE, subject=subject, resource=obj, relation=relation)
            raise ForbiddenError(f"{subject} lacks {relation} on {obj}")

    return Depends(_gate)


#: The decorator dependencies an annotation write and an annotation read carry.
MAY_ANNOTATE = project_gate("can_annotate")
MAY_VIEW = project_gate("can_view")


def project_annotations(request: Request, project: ProjectParam, settings: SettingsDep, tokens: ServiceTokenDep) -> Callable[[str], ProjectAnnotations]:
    """The project's annotation tables, reached as the workbench's own identity."""
    state = request.app.state.resources
    token = tokens.for_service if tokens is not None else None

    def _for(dataset_id: str) -> ProjectAnnotations:
        return ProjectAnnotations.of(state.settings, settings.annotations_namespace, project, dataset_id, token)

    return _for


AnnotationsDep = Annotated[Callable[[str], ProjectAnnotations], Depends(project_annotations)]


async def _reads_dataset(request: Request, subject: CurrentSubject, checker: CheckerDep, state: StateDep) -> None:
    await require_read(state.settings.sources, checker, subject, named_in(request, state.settings.default_dataset))


#: The decorator dependency a route reading a corpus carries: the person may read its source, where the workbench decides.
READS_DATASET = Depends(_reads_dataset)
