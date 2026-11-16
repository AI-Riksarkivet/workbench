"""S4 — the create pin: creating an annotation project is authorized on the PARENT organisation.

The door is `can_create_project` on `org:<tenant>`: the child does not exist yet, so nobody holds a role on it. The
checker is injected; the project is written to a PostgreSQL database of the test's own (`state_app`) and read back
from it, so a create that answers 201 without writing anything cannot pass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints.projects import CREATE_RELATION, router


def _app(state_app: Callable[..., FastAPI], *, allow: bool) -> FastAPI:
    """An app whose FGA checker answers `allow`."""

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        return allow

    app = state_app(router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    # The VERIFIED subject. Overriding current_subject (not a header) is the point: with OIDC on
    # there is no header path to this value at all.
    app.dependency_overrides[current_subject] = lambda: "gina"
    return app


@pytest.fixture
def client(state_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    with TestClient(_app(state_app, allow=True)) as c:
        yield c


def _stored(client: TestClient) -> list[dict[str, Any]]:
    """Every project of the tenant, as the store lists them."""
    return client.get("/projects", params={"tenant": "acme"}).json()["projects"]


PAYLOAD = {"tenant": "acme", "slug": "labels-2026", "title": "Labels 2026"}


def test_a_tenant_member_creates_the_project_201(client: TestClient) -> None:
    r = client.post("/projects", json=PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["tenant"], body["slug"], body["created_by"]) == ("acme", "labels-2026", "gina")
    # Born in draft: `open` is a separate can_manage transition, so creating never implies ready.
    assert body["state"] == "draft"
    assert [p["project_id"] for p in _stored(client)] == [body["project_id"]]


def test_a_non_member_is_denied_403(state_app: Callable[..., FastAPI]) -> None:
    with TestClient(_app(state_app, allow=False)) as client:
        r = client.post("/projects", json=PAYLOAD)
    assert r.status_code == 403, r.text
    assert CREATE_RELATION in r.json()["detail"]


def test_tenant_is_required_so_the_door_can_never_be_inferred(client: TestClient) -> None:
    """`tenant` IS the authz parent — a request that omits it must be rejected, never guessed."""
    payload = {k: v for k, v in PAYLOAD.items() if k != "tenant"}
    assert client.post("/projects", json=payload).status_code == 422


def test_consensus_n_travels_from_the_create_payload_to_the_stored_project(client: TestClient) -> None:
    r = client.post("/projects", json={**PAYLOAD, "consensus_n": 3})
    assert r.status_code == 201, r.text
    assert [p["consensus_n"] for p in _stored(client)] == [3], "the field must PERSIST, not merely echo"


def test_consensus_n_defaults_to_one_and_out_of_range_is_422(client: TestClient) -> None:
    assert client.post("/projects", json=PAYLOAD).json()["consensus_n"] == 1
    assert client.post("/projects", json={**PAYLOAD, "consensus_n": 6}).status_code == 422
    assert client.post("/projects", json={**PAYLOAD, "consensus_n": 0}).status_code == 422
    assert [p["consensus_n"] for p in _stored(client)] == [1], "a rejected value must never persist"


def test_an_ontology_that_contradicts_itself_is_refused_at_create(client: TestClient) -> None:
    """One document means the ontology validates itself, and the create endpoint gets that for free: a relation naming
    an undeclared class is refused, and nothing is stored."""
    r = client.post(
        "/projects",
        json={
            **PAYLOAD,
            "ontology": {
                "classes": [{"name": "key"}],
                "relations": [{"name": "answers", "from_classes": ["key"], "to_classes": ["value"]}],
            },
        },
    )
    assert r.status_code == 422, r.text
    assert "value" in r.text, "the refusal must name the undeclared class"
    assert _stored(client) == [], "a project was persisted despite a contradictory ontology"
