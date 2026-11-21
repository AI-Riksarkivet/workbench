"""Who may see and run a flow: the roles of the project it belongs to, in the workbench's own store.

A flow is project-scoped. Owner and manager create, edit and run a project's flows; reviewer and annotator run them;
a viewer reads the flow and its runs (`workbench/governed/auth/model.fga`, `type project`'s `can_*_flow`). Every route
but the node catalog names its project in the path, and the check is asked on `project:<project>`: no flow is stored
server-side (the studio keeps the graph), so the project is the object a flow's permissions come from.

Everything mechanical (token and proof to a verified person, the checker) comes from `workbench.governed.deps`. With
authorization off every check is allowed, so a dev stack behaves as before.
"""

from typing import Annotated

from fastapi import Depends

from flows.audit_actions import FlowsAuditAction
from flows.dependencies import FlowsSettingsDep
from workbench.exceptions import ForbiddenError
from workbench.governed.audit import AuditOutcome, audit
from workbench.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(FlowsSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]
#: A verified person, for the one route that names no project.
AUTHENTICATED = Depends(_deps.current_subject)

#: Read the flow and its runs (viewer and up).
READ = "can_read_flow"
#: Start or stop a run (annotator and up).
RUN = "can_run_flow"


async def require(checker: FgaChecker, subject: str, relation: str, project: str, action: FlowsAuditAction) -> None:
    """Refuse a caller who lacks ``relation`` on the project, naming what is missing; audit either way."""
    obj = f"project:{project}"
    allowed = await checker(user=subject, relation=relation, obj=obj)
    audit(action, AuditOutcome.ALLOW if allowed else AuditOutcome.DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{relation}' on '{obj}'")
