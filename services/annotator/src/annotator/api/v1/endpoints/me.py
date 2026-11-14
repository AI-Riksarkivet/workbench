"""Who the caller is to the workbench: the identity its navbar renders, from the workbench's own store.

The person the request's token proves, and their standing in each organisation the workbench's store holds them in
(`admin` or `member`). ``estate_admin`` is whether they administer any organisation: the workbench answers it, never a
lakehouse.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from annotator.api.security import AuthorizerDep, CurrentToken


router = APIRouter(tags=["identity"])


class OrgStanding(BaseModel):
    #: The organisation (the tenant its projects name).
    org: str
    role: Literal["admin", "member"]


class Me(BaseModel):
    sub: str
    name: str | None = None
    email: str | None = None
    estate_admin: bool
    orgs: list[OrgStanding]


@router.get("/me")
async def me(caller: CurrentToken, authz: AuthorizerDep) -> Me:
    """The caller and their organisations; anonymous, with every role, when authentication and authorization are off."""
    if caller is None:
        return Me(sub="anon", estate_admin=authz is None, orgs=[])
    claims = caller.claims.model_extra or {}
    admin: list[str] = []
    member: list[str] = []
    if authz is not None:
        admin = await authz.list_objects(user=caller.subject, relation="can_administer", object_type="org")
        member = await authz.list_objects(user=caller.subject, relation="can_list_projects", object_type="org")
    standing: list[OrgStanding] = [OrgStanding(org=org.removeprefix("org:"), role="admin") for org in admin]
    standing += [OrgStanding(org=org.removeprefix("org:"), role="member") for org in member if org not in admin]
    standing.sort(key=lambda s: s.org)
    return Me(
        sub=caller.subject,
        name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        estate_admin=bool(admin),
        orgs=standing,
    )
