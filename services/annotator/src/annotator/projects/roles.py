"""A project's five roles, owner > manager > reviewer > annotator > viewer, as grants in the workbench's own store.

What each role may do is the model's (`service_kit/governed/auth/model.fga`, `type project`): the roles are
concentric, and each `can_*` an annotator route asks names its least role there. A reviewer reaches the annotator's
actions because a reviewer must be able to `fix_and_accept`, which IS annotating; publishing is the manager's act.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict


#: A grantable role on one project, most privileged first. A plain alias rather than a `type` statement, because a
#: route takes it as a query parameter.
Role = Literal["owner", "manager", "reviewer", "annotator", "viewer"]
GRANTABLE: Final[tuple[str, ...]] = get_args(Role)
#: The roles that may grant others. Losing every one leaves the project manageable only by an org admin.
ADMINISTRATIVE: Final[frozenset[str]] = frozenset({"owner", "manager"})


class MemberGrant(BaseModel):
    """One direct grant on a project: a person and the role it gives them."""

    model_config = ConfigDict(frozen=True)

    #: The person's principal key, `<idp-id>~<subject>`.
    user: str
    relation: Role
