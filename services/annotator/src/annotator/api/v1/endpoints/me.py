"""Who the caller is to the workbench: the identity its navbar renders, from the workbench's own store.

The person the request's token proves, and their standing in each organisation the workbench's store holds them in
(`admin` or `member`). ``estate_admin`` is whether they administer any organisation: the workbench answers it, never a
lakehouse.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from annotator.api.security import AuthorizerDep, CurrentToken, SettingsDep
from annotator.people import display_name, register_person


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


#: The one organisation an unauthenticated local stack names, so its anonymous person can create a project and label.
LOCAL_ORGANISATION: Final = "local"


@router.get("/me")
async def me(request: Request, caller: CurrentToken, authz: AuthorizerDep, settings: SettingsDep) -> Me:
    """The caller and their organisations, and the caller's name registered (`people.register_person`): the app reads
    this once each time a person signs in, which is what lets everyone else see them by name before they write.

    With authentication off — only ever with `WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED`, which the settings refuse beside
    OIDC — the person is anonymous, holds every role (authorization is off too) and administers `LOCAL_ORGANISATION`.
    An authenticated person's organisations are the store's answer alone.
    """
    if caller is None:
        local = [OrgStanding(org=LOCAL_ORGANISATION, role="admin")] if settings.insecure_allow_unauthenticated else []
        return Me(sub="anon", estate_admin=authz is None, orgs=local)
    claims = caller.claims.model_extra or {}
    await register_person(request, caller)
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
        name=display_name(claims),
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        estate_admin=bool(admin),
        orgs=standing,
    )
