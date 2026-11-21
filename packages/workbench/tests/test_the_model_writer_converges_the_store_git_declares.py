"""The model writer makes the workbench's store hold this image's model, the organisations git declares, and each
source's readers.

Against the pinned OpenFGA (root `conftest.py::openfga`), through the writer's own entry (`converge`) and the lookup
every service makes at boot (`fga.build_authorizer`): a second run with nothing changed writes nothing, a declaration
that drops a person removes their grant (an organisation's or a source's), and a service resolves the model the writer
wrote and answers on it.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.openfga import OpenFgaServer
from workbench.governed import fga
from workbench.governed.auth.write_model import Declaration, WriterSettings, converge, main


def _declared(admins: list[str], members: list[str], source_readers: list[str]) -> Declaration:
    return Declaration.model_validate(
        {
            "orgs": {"acme": {"admins": admins, "members": members}},
            "sources": [{"name": "archive", "kind": "namespace", "readers": {"orgs": ["acme"], "people": source_readers}}, {"name": "scans"}],
        }
    )


def test_the_writer_converges_and_a_service_answers_on_what_it_wrote(openfga: OpenFgaServer) -> None:
    settings = WriterSettings.model_validate(
        {
            "WORKBENCH_FGA_API_URL": openfga.url,
            "WORKBENCH_FGA_STORE": f"test-{uuid.uuid4().hex}",
            "WORKBENCH_FGA_ORGS_FILE": "orgs.json",
            "WORKBENCH_SOURCES_FILE": "sources.json",
        }
    )

    crowd = [f"keycloak~reader-{n}" for n in range(150)]
    first = asyncio.run(converge(settings, _declared(["keycloak~alice"], ["keycloak~bob", "keycloak~carol", *crowd], ["keycloak~dora"])))
    again = asyncio.run(converge(settings, _declared(["keycloak~alice"], ["keycloak~bob"], [])))

    assert (again.store_id, again.model_id) == (first.store_id, first.model_id)
    # More grants than one OpenFGA write carries, written and then deleted in chunks.
    assert len(first.written) == 155
    assert {("user:keycloak~alice", "admin"), ("user:keycloak~bob", "member"), ("user:keycloak~carol", "member")} <= {
        (g.user, g.relation) for g in first.written
    }
    assert (again.written, sorted(g.user for g in again.deleted)) == ([], sorted(["user:keycloak~carol", "user:keycloak~dora", *(f"user:{p}" for p in crowd)]))

    async def _answers() -> dict[str, list[bool]]:
        authz = await fga.build_authorizer(openfga.url, credential_file=None, store_name=settings.store, model_id=None)
        assert authz is not None
        try:
            return {
                person: [
                    await authz.check(user=person, relation="can_create_project", obj="org:acme"),
                    await authz.check(user=person, relation="can_read", obj="source:archive"),
                    await authz.check(user=person, relation="can_read", obj="source:scans"),
                ]
                for person in ("keycloak~bob", "keycloak~carol", "keycloak~dora")
            }
        finally:
            await authz.close()

    assert asyncio.run(_answers()) == {"keycloak~bob": [True, True, False], "keycloak~carol": [False, False, False], "keycloak~dora": [False, False, False]}


@pytest.mark.parametrize("orgs_file", [None, "absent.json"], ids=["unset", "unreadable"])
def test_a_writer_with_no_readable_declaration_fails_rather_than_revoke_every_org_grant(
    openfga: OpenFgaServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, orgs_file: str | None
) -> None:
    monkeypatch.setenv("WORKBENCH_FGA_API_URL", openfga.url)
    monkeypatch.setenv("WORKBENCH_FGA_STORE", f"test-{uuid.uuid4().hex}")
    if orgs_file is None:
        monkeypatch.delenv("WORKBENCH_FGA_ORGS_FILE", raising=False)
    else:
        monkeypatch.setenv("WORKBENCH_FGA_ORGS_FILE", str(tmp_path / orgs_file))

    assert main() == 1
