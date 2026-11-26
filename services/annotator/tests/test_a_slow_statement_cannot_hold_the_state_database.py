"""A statement that outlasts the annotator's statement timeout is cancelled by its database, and the door that sent it
answers a clear error rather than holding a connection, a row lock or a request without end.

Driven over HTTP against the annotator's own doors on a real PostgreSQL of the test's own: a second connection, as
another request would, holds the task's row, so the door's statement waits on it past a timeout made short for the test.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import psycopg
import pytest

from annotator.core.config import get_annotator_settings
from tests.model_item import Build
from tests.postgres import StateDatabase


#: How long a statement may run here: short, so a held row lock outlasts it, and long enough that the test's own
#: seeding and every commit stay well inside it on a loaded host.
TIMEOUT_SECONDS = "1"
#: When the other request lets the row go, so a door with no timeout answers late rather than never.
HOLD_SECONDS = 5.0


@pytest.fixture
def short_statements(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The annotator's settings with a short statement timeout, set before the app reads them."""
    monkeypatch.setenv("ANNOTATOR_STATE_STATEMENT_TIMEOUT_SECONDS", TIMEOUT_SECONDS)
    get_annotator_settings.cache_clear()
    yield
    get_annotator_settings.cache_clear()


def test_a_statement_held_past_its_timeout_is_cancelled_and_the_door_says_so(short_statements: None, client_for: Build, state_database: StateDatabase) -> None:
    client = client_for(None)
    env = state_database.env()
    holder = psycopg.connect(
        host=env["ANNOTATOR_STATE_HOST"], port=int(env["ANNOTATOR_STATE_PORT"]), dbname=env["ANNOTATOR_STATE_DATABASE"], user=env["ANNOTATOR_STATE_USER"]
    )
    holder.execute("SELECT 1 FROM tasks WHERE task_id = 't1' FOR UPDATE")
    letting_go = threading.Timer(HOLD_SECONDS, holder.rollback)
    letting_go.start()
    try:
        held = client.post("/tasks/t1/events", json={"event": "release"})
    finally:
        letting_go.join()
        holder.close()
    freed = client.post("/tasks/t1/events", json={"event": "release"})

    assert (held.status_code, held.headers["content-type"]) == (503, "application/problem+json")
    assert held.json()["type"] == "about:blank#statement-cancelled"
    assert held.json()["detail"] == (
        "the annotator's database stopped this request at its 1 s limit; it may have been waiting on another request's "
        "change to the same item. Nothing was changed; try again."
    )
    assert freed.status_code == 200, freed.text
