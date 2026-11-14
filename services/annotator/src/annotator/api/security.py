"""Who the request is, and what the workbench's own store lets them do in the annotator.

Who: a person verified by the shared door (`service_kit.governed.deps`), a DPoP-bound token addressed to the workbench.

What: the workbench's own OpenFGA model (`service_kit/governed/auth/model.fga`): an organisation's admins and members,
a project's five concentric roles (owner > manager > reviewer > annotator > viewer), and the tasks and corpora that
inherit from a project. The annotator never asks the lakehouse what a person may do in the annotator.

Data: the lakehouse decides. A corpus is read as the person, on a token the annotator exchanges for them (`CallerDep`),
and annotations are written as the workbench's own identity once the annotator's own role check has passed, naming
the person in the commit (`annotator.annotations.commit`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Query, Request

from annotator.annotations.store import ProjectAnnotations
from annotator.core.config import AnnotatorSettings, get_annotator_settings
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.deps import ANONYMOUS_SUBJECT, FgaChecker, make_auth_deps
from service_kit.governed.fga import Authorizer
from service_kit.governed.identity import Caller
from service_kit.governed.token_exchange import LakehouseCaller, LakehouseTokens


__all__ = [
    "ANONYMOUS_SUBJECT",
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
    if tokens is None and settings.catalog_uri:
        raise ServiceUnavailableError("the annotator names no confidential client to write the lakehouse as itself")
    return tokens


ServiceTokenDep = Annotated[LakehouseTokens | None, Depends(service_token)]


#: The project a corpus read or an annotation write is made for, named by the request: its roles decide.
ProjectParam = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="The project this annotation work is for")]


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
