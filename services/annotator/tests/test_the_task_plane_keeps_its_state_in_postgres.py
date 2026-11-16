"""The project and task plane runs on the annotator's own PostgreSQL, through the service the pod serves.

The module-level app (`annotator.main.app`) with its own lifespan, on a database of the test's own; sign-in and
authorization are off, as on the local stack, and the person each request is made as is named with the
`current_subject` override. A person labels an item from a project's creation to its review, and what they did is
read back after the service restarts.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from annotator.api.security import current_subject
from annotator.core.config import AnnotatorSettings
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import Task
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase


class _Person:
    """Who the next request is made as."""

    def __init__(self) -> None:
        self.name = "alice"


@pytest.fixture
def person() -> _Person:
    return _Person()


@pytest.fixture
def service(state_settings: AnnotatorSettings, person: _Person) -> Iterator[Any]:
    """Open the service on the test's database; calling the yielded function opens it again, as a restart does."""
    from annotator.main import app

    app.dependency_overrides[current_subject] = lambda: person.name
    clients: list[TestClient] = []

    def start() -> TestClient:
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield start
    for client in clients:
        client.__exit__(None, None, None)
    app.dependency_overrides.clear()


def _as(client: TestClient, person: _Person, name: str) -> TestClient:
    person.name = name
    return client


def _project_with_one_task(client: TestClient, person: _Person, *, lease_seconds: int = 1800) -> tuple[str, str]:
    created = _as(client, person, "alice").post("/projects", json={"tenant": "local", "slug": "letters", "lease_seconds": lease_seconds})
    assert created.status_code == 201, created.text
    project = created.json()["project_id"]
    assert client.post(f"/projects/{project}/events", json={"event": "open"}).status_code == 200
    sent = client.post(
        f"/projects/{project}/items", json={"items": [{"task_id": "t1", "source": {"kind": "chunks", "keys": ["d1"]}, "media": {"kind": "image"}}]}
    )
    assert sent.status_code == 201, sent.text
    return project, "t1"


def test_a_task_is_labelled_from_claim_to_review_and_the_state_survives_a_restart(service: Any, person: _Person) -> None:
    client = service()
    project, task = _project_with_one_task(client, person)
    box = {"shape_type": "bbox", "label": "seal", "x": 1, "y": 2, "width": 3, "height": 4}

    claimed = _as(client, person, "alice").post(f"/tasks/{task}/events", json={"event": "claim"})
    saved = client.put(f"/tasks/{task}/draft", json={"shapes": [box]})
    submitted = client.post(f"/tasks/{task}/events", json={"event": "submit"})
    accepted = _as(client, person, "bob").post(f"/tasks/{task}/events", json={"event": "accept"})
    assert [r.status_code for r in (claimed, saved, submitted, accepted)] == [200, 200, 200, 200], [r.text for r in (claimed, saved, submitted, accepted)]

    client.__exit__(None, None, None)
    restarted = service()
    read = restarted.get(f"/tasks/{task}").json()
    draft = restarted.get(f"/tasks/{task}/draft").json()
    listing = restarted.get(f"/projects/{project}/tasks").json()

    assert (read["state"], read["submitted_by"], read["reviewed_by"], read["review_action"]) == ("accepted", "alice", "bob", "accepted")
    assert [(t["event"], t["by"]) for t in read["transitions"]] == [("claim", "alice"), ("submit", "alice"), ("accept", "bob")]
    assert (draft["revision"], draft["author"], [(s["shape_type"], s["label"]) for s in draft["shapes"]]) == (1, "alice", [("bbox", "seal")])
    assert (listing["tasks"], listing["terminal"], listing["may_publish"]) == ({"t1": "accepted"}, 1, True)


def test_of_two_concurrent_claims_of_one_task_exactly_one_wins(service: Any, person: _Person, state_database: StateDatabase) -> None:
    """The overlap is forced, not hoped for: a third connection holds the task's row until both claims wait on it, so
    both have read the task before either writes, and only the row lock decides."""
    client = service()
    _project, task = _project_with_one_task(client, person)
    from annotator.main import app

    store: AnnotatorStore = app.state.store
    portal = client.portal
    assert portal is not None

    async def claim(actor: str) -> Task | IllegalTransition:
        try:
            return await store.fire_task(task, event="claim", actor=actor)
        except IllegalTransition as refused:
            return refused

    conninfo = f"host={state_database.host} port={state_database.port} user={state_database.user} dbname={state_database.database}"
    with psycopg.connect(conninfo) as holder:
        holder.execute("SELECT 1 FROM tasks WHERE task_id = %s FOR UPDATE", (task,))
        claims = [portal.start_task_soon(claim, actor) for actor in ("alice", "bob")]
        with psycopg.connect(conninfo, autocommit=True) as watcher:
            deadline = time.monotonic() + 10
            while _waiting_on_a_lock(watcher, state_database.database) < 2:
                assert time.monotonic() < deadline, "the two claims never both waited on the task's row"
                time.sleep(0.02)
        holder.commit()
    outcomes = [future.result(timeout=10) for future in claims]
    held = client.get(f"/tasks/{task}").json()

    assert sorted(type(o).__name__ for o in outcomes) == ["IllegalTransition", "Task"]
    winner = next(o for o in outcomes if isinstance(o, Task))
    assert (held["assignee"], [t["event"] for t in held["transitions"]]) == (winner.assignee, ["claim"])


def _waiting_on_a_lock(conn: psycopg.Connection[Any], database: str) -> int:
    row = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND wait_event_type = 'Lock'", (database,)).fetchone()
    return int(row[0]) if row else 0


def test_a_claim_whose_lease_ran_out_is_released_to_the_next_person(service: Any, person: _Person) -> None:
    client = service()
    project, task = _project_with_one_task(client, person, lease_seconds=1)
    assert _as(client, person, "alice").post(f"/tasks/{task}/events", json={"event": "claim"}).status_code == 200
    expires = client.get(f"/tasks/{task}").json()["lease_expires_at"]

    refused_while_held = _as(client, person, "bob").post(f"/tasks/{task}/events", json={"event": "claim"})
    deadline = time.monotonic() + 10
    # Read off the project's listing, which the database answers on its own clock (`task_states`).
    while client.get(f"/projects/{project}/tasks").json()["tasks"][task] != "unassigned":
        assert time.monotonic() < deadline, "the lapsed claim was never read as released"
        time.sleep(0.1)
    taken = client.post(f"/tasks/{task}/events", json={"event": "claim"})

    assert refused_while_held.status_code == 409
    assert taken.status_code == 200, taken.text
    assert taken.json()["assignee"] == "bob"
    trail = [(t["event"], t["by"], t["at"]) for t in taken.json()["transitions"]]
    assert [(event, by) for event, by, _at in trail] == [("claim", "alice"), ("lease_expired", "system"), ("claim", "bob")]
    assert datetime.fromisoformat(trail[1][2]) == datetime.fromisoformat(expires)


def test_a_service_naming_no_state_database_refuses_the_project_plane_and_stays_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from annotator.core.config import get_annotator_settings
    from annotator.main import app

    monkeypatch.delenv("ANNOTATOR_STATE_HOST", raising=False)
    get_annotator_settings.cache_clear()
    try:
        with TestClient(app) as client:
            listing = client.get("/projects", params={"tenant": "local"})
            ready = client.get("/readyz")
    finally:
        get_annotator_settings.cache_clear()

    assert (listing.status_code, listing.json()["detail"]) == (
        503,
        "the project and task plane is unavailable: the annotator names no state database (ANNOTATOR_STATE_HOST is unset)",
    )
    assert (ready.status_code, ready.json()["components"]) == (200, {"state": "unconfigured"})


def test_the_local_person_belongs_to_the_local_organisation(service: Any) -> None:
    client = service()

    assert client.get("/me").json() == {"sub": "anon", "name": None, "email": None, "estate_admin": True, "orgs": [{"org": "local", "role": "admin"}]}
