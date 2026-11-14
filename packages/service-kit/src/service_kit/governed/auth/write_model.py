"""The model writer: make the workbench's OpenFGA store hold this image's model and the organisations git declares.

The deployment runs it as a Job before the services roll (the annotator fixture's ``openfga-model`` hook), so a new
image's model exists before its pods look for it, and a service never writes a model itself. It converges:

* the store named ``WORKBENCH_FGA_STORE`` exists;
* a model carrying this image's ``model.json`` is in it (written only when none carries the body, so an unchanged
  model mints no new version);
* each organisation's ``admin`` and ``member`` tuples are exactly what ``WORKBENCH_FGA_ORGS_FILE`` declares: a declared
  one missing is written, and one present that the file does not declare is deleted. The file is required: an unset
  or unreadable one fails the Job rather than declare nothing and revoke every org grant.

Run again with nothing changed, it writes nothing. The store's preshared key is read from the mounted
``WORKBENCH_FGA_CREDENTIAL_FILE``, never from the environment.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Final

from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.fga import DEFAULT_STORE_NAME, MAX_TUPLES_PER_WRITE, Authorizer, Grant, make_client, provision, user_object
from service_kit.governed.principal import PrincipalKey


log = logging.getLogger(__name__)

#: The relations on an `org` the declaration owns; every other tuple in the store is a door's to write.
ORG_RELATIONS: Final = frozenset({"admin", "member"})
#: A bound on the pages of the store's tuples one run reads: past it the run fails rather than converge on part.
MAX_READ_PAGES: Final = 10_000


class OrgDeclaration(BaseModel):
    """Who administers one organisation and who belongs to it, by principal key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admins: list[PrincipalKey] = Field(default_factory=list)
    members: list[PrincipalKey] = Field(default_factory=list)


class Declaration(BaseModel):
    """The organisations git declares, keyed by org id (the tenant a project names)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    orgs: dict[str, OrgDeclaration] = Field(default_factory=dict)

    def grants(self) -> set[Grant]:
        return {
            Grant(user=user_object(person), relation=relation, object=f"org:{org}")
            for org, declared in self.orgs.items()
            for relation, people in (("admin", declared.admins), ("member", declared.members))
            for person in people
        }


class WriterSettings(BaseSettings):
    """The writer's settings, from the Job's environment."""

    model_config = SettingsConfigDict(populate_by_name=True)

    api_url: str = Field(alias="WORKBENCH_FGA_API_URL")
    store: str = Field(default=DEFAULT_STORE_NAME, alias="WORKBENCH_FGA_STORE")
    credential_file: str | None = Field(default=None, alias="WORKBENCH_FGA_CREDENTIAL_FILE")
    #: Required: an absent declaration must fail the Job, never read as "no organisations" and revoke every org grant.
    orgs_file: Path = Field(alias="WORKBENCH_FGA_ORGS_FILE")


class Converged(BaseModel):
    """What one run changed."""

    store_id: str
    model_id: str
    written: list[Grant]
    deleted: list[Grant]


async def converge(settings: WriterSettings, declaration: Declaration) -> Converged:
    """Make the store hold the model and exactly the declared organisation grants; return what changed."""
    store_id, model_id = await provision(settings.api_url, credential_file=settings.credential_file, store_name=settings.store)
    client = make_client(settings.api_url, credential_file=settings.credential_file, store_id=store_id, model_id=model_id)
    authz = Authorizer(client)
    try:
        held: set[Grant] = set()
        token = ""
        for _ in range(MAX_READ_PAGES):
            options: dict[str, int | str | dict[str, int | str]] = {"page_size": 100} | ({"continuation_token": token} if token else {})
            page = await client.read(ReadRequestTupleKey(), options=options)
            held |= {
                Grant(user=t.key.user, relation=t.key.relation, object=t.key.object)
                for t in page.tuples or []
                if t.key.object.startswith("org:") and t.key.relation in ORG_RELATIONS
            }
            token = page.continuation_token or ""
            if not token:
                break
        else:
            raise RuntimeError(f"the store's tuples run past {MAX_READ_PAGES} pages; refusing to converge on part of them")
        wanted = declaration.grants()
        missing, undeclared = sorted(wanted - held, key=str), sorted(held - wanted, key=str)
        # One write may carry at most MAX_TUPLES_PER_WRITE tuples; a converge that stops part-way is finished by the
        # next run, which reads what landed.
        for start in range(0, len(missing), MAX_TUPLES_PER_WRITE):
            await authz.write(missing[start : start + MAX_TUPLES_PER_WRITE])
        for start in range(0, len(undeclared), MAX_TUPLES_PER_WRITE):
            await authz.delete(undeclared[start : start + MAX_TUPLES_PER_WRITE])
    finally:
        await authz.close()
    return Converged(store_id=store_id, model_id=model_id, written=missing, deleted=undeclared)


def main() -> int:
    """The Job's entrypoint: converge, print what changed, exit 0; a failure exits 1 and the deployment retries it."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        settings = WriterSettings()
        declaration = Declaration.model_validate_json(settings.orgs_file.read_text())
    except (OSError, ValueError):
        log.exception("the model writer has no readable declaration (WORKBENCH_FGA_ORGS_FILE); refusing to converge on none")
        return 1
    try:
        result = asyncio.run(converge(settings, declaration))
    except Exception:
        log.exception("the model writer could not converge store %r at %s", settings.store, settings.api_url)
        return 1
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":  # pragma: no cover - the Job's entrypoint
    raise SystemExit(main())
