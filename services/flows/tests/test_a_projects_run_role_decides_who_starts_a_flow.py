"""A flow's run is started by the project's run role, in the workbench's own store, and read by its read role.

The flows app with its real lifespan, signed in through the real door (a DPoP-bound token the loopback provider issued,
and a proof per request), authorizing against a store of the test's own on the pinned OpenFGA, with the project's grants
written as the annotator's create and members doors write them. Every outbound HTTP call goes through a strict respx
router that passes only the identity provider: nothing in the path asks a lakehouse what a person may do.

Project p1: carol owns it, fay annotates, gus views it. Fay annotates project p2 too.
"""

from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient

from flows.lifespan import make_lifespan
from flows.routes import router
from service_kit import make_service_app
from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, BrowserKey, Provider
from tests.openfga import OpenFgaStore


BODY = {"graph": {"nodes": [{"id": "t", "kind": "text"}, {"id": "s", "kind": "inspect"}], "edges": [{"source": "t", "target": "s"}]}, "seeds": {"t": "hi"}}


class _Person:
    def __init__(self, provider: Provider, name: str) -> None:
        self.key = BrowserKey()
        self.token = provider.mint(name, jkt=self.key.jkt)

    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"Authorization": f"DPoP {self.token}", "DPoP": self.key.proof(method, f"http://testserver{path}", self.token)}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, oidc_provider: Provider, openfga_store: OpenFgaStore) -> Iterator[TestClient]:
    for name, value in {
        "WORKBENCH_OIDC_ENABLED": "true",
        "WORKBENCH_OIDC_ISSUER": oidc_provider.issuer,
        "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
        "WORKBENCH_OIDC_IDP_ID": IDP_ID,
        "WORKBENCH_OIDC_ALLOW_INSECURE": "true",
        "WORKBENCH_FGA_ENABLED": "true",
        "WORKBENCH_FGA_API_URL": openfga_store.url,
        "WORKBENCH_FGA_STORE": openfga_store.name,
        "WORKBENCH_API_PREFIX": "/api",
        "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    openfga_store.write(
        ("user:keycloak~carol", "owner", "project:p1"),
        ("user:keycloak~fay", "annotator", "project:p1"),
        ("user:keycloak~gus", "viewer", "project:p1"),
        ("user:keycloak~fay", "annotator", "project:p2"),
    )
    app = make_service_app(title="flows", routers=[router], lifespan=make_lifespan)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as outbound:
        outbound.route(host="127.0.0.1", port=int(oidc_provider.issuer.rsplit(":", 1)[1])).pass_through()
        with TestClient(app) as test_client:
            yield test_client


def _start(client: TestClient, person: _Person, project: str) -> tuple[int, dict[str, object]]:
    path = f"/api/flows/projects/{project}/runs"
    response = client.post(path, json=BODY, headers=person.headers("POST", path))
    return response.status_code, response.json()


def test_the_run_role_starts_a_flow_and_a_viewer_is_refused(client: TestClient, oidc_provider: Provider) -> None:
    fay, gus = _Person(oidc_provider, "fay"), _Person(oidc_provider, "gus")

    started, refused = _start(client, fay, "p1"), _start(client, gus, "p1")

    assert started[0] == 200, started
    assert refused == (403, refused[1])
    assert refused[1]["detail"] == "'keycloak~gus' lacks 'can_run_flow' on 'project:p1'"


def test_a_run_is_read_by_its_projects_read_role_and_by_no_other_project(client: TestClient, oidc_provider: Provider) -> None:
    fay, gus, mallory = (_Person(oidc_provider, name) for name in ("fay", "gus", "mallory"))
    status, run = _start(client, fay, "p1")
    assert status == 200, run
    in_p1, in_p2 = f"/api/flows/projects/p1/runs/{run['run_id']}", f"/api/flows/projects/p2/runs/{run['run_id']}"

    by_viewer = client.get(in_p1, headers=gus.headers("GET", in_p1))
    by_stranger = client.get(in_p1, headers=mallory.headers("GET", in_p1))
    as_another_project = client.get(in_p2, headers=_Person(oidc_provider, "fay").headers("GET", in_p2))

    assert (by_viewer.status_code, by_viewer.json()["project"]) == (200, "p1")
    assert by_stranger.status_code == 403
    assert as_another_project.status_code == 404
