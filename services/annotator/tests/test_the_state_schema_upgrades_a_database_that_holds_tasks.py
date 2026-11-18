"""A database the previous annotator filled upgrades in place at the next start, and the data manager reads it.

The schema is applied at every start (`annotator.state.store.apply_schema`), so an upgrade is that apply over rows an
earlier release wrote: the listing's columns are generated from each document by Postgres, so the rows already there get
them as the columns are added, with no step that rewrites a document. Driven on the real PostgreSQL fixture: the previous
release's schema (`fixtures/schema_before_the_data_manager.sql`, as the release before this one shipped it) and documents
shaped as its code wrote them (no `sent_at`; one item never touched, so with no transition at all; one whose instant
carries no offset), then this release's schema applied twice, under a session TimeZone far from UTC, then the listing
over HTTP.

An item an earlier release sent records no send instant. Its `sent` is unknown, and answered as unknown (it sorts last
and falls outside a range on the send date), never filled from its first transition, which is a claim's instant and
would place the item in a range it was not sent in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from string import Template
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import projects, tasks
from annotator.api.v1.endpoints.project_events import router
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import ProjectState, TaskState
from annotator.state.store import AnnotatorStore
from annotator.text import MAX_ID
from tests.postgres import StateDatabase


PREVIOUS = Path(__file__).parent / "fixtures" / "schema_before_the_data_manager.sql"
SUBMITTED = "2026-11-10T09:30:00Z"
LONG_ID = "y" * (MAX_ID + 904)


def _task(task_id: str, state: str, **over: Any) -> dict[str, Any]:
    """A task document as the previous release wrote it: every field its model held, and no `sent_at`."""
    return {
        "task_id": task_id,
        "project_id": "p1",
        "state": state,
        "assignee": None,
        "lease_expires_at": None,
        "source": {"kind": "chunks", "keys": [task_id], "where": "lake~acme$pages", "dataset_version": None, "table": None},
        "media": {"kind": "image", "width": None, "height": None},
        "review_required": True,
        "lease_seconds": 1800,
        "ontology": {"kind": "", "modality": "image", "classes": [], "relations": [], "allow_empty": False},
        "prediction": [],
        "replica_of": None,
        "holders": [],
        "submitted_by": None,
        "submitted_at": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_action": None,
        "review_notes": [],
        "transitions": [],
        "transitions_dropped": 0,
        "review_notes_dropped": 0,
        "lead_time_seconds": 0.0,
        "skipped_reason": None,
        **over,
    }


def _fill_as_the_previous_release(database: StateDatabase) -> None:
    schema = Template(PREVIOUS.read_text()).substitute(
        project_states=", ".join(f"'{s}'" for s in ProjectState), task_states=", ".join(f"'{s}'" for s in TaskState)
    )
    # Past every bound a request is now held to (a class's colour, a source's keys, a shape id, a link's name), as an
    # earlier release took them: a stored document is read as it was written.
    ontology = {"classes": [{"name": "seal", "colour": "c" * 100}]}
    project = {"project_id": "p1", "tenant": "acme", "warehouse": "", "slug": "vasa", "state": "labeling", "consensus_n": 1, "ontology": ontology}
    claim = {"at": "2026-11-10T09:00:00Z", "by": "dex~ann", "event": "claim", "from": "unassigned", "to": "claimed"}
    submit = {"at": SUBMITTED, "by": "dex~ann", "event": "submit", "from": "claimed", "to": "in_review"}
    tasks = [
        _task("a-untouched", "unassigned", source={"kind": "chunks", "keys": [f"k{i}-" + "k" * 5000 for i in range(70)], "where": "w" * 600}),
        _task("b-submitted", "in_review", holders=["dex~ann"], submitted_by="dex~ann", submitted_at=SUBMITTED, transitions=[claim, submit]),
        # An instant with no offset: the store never stamps one, and the models would carry one written by hand. It names
        # no instant without the session's TimeZone, so it is read as none, whatever the session.
        _task("c-no-offset", "in_review", holders=["dex~bo"], submitted_by="dex~bo", submitted_at="2026-11-10T09:45:00"),
    ]
    draft = {
        "task_id": "b-submitted",
        "project_id": "p1",
        "author": "dex~ann",
        "revision": 1,
        "links": [{"name": "n" * 600, "from_shape": "s1", "to_shape": LONG_ID}],
        "origin": "human",
        "shapes": [{"shape_id": "s1", "shape_type": "bbox", "label": "seal"}, {"shape_id": LONG_ID, "shape_type": "bbox", "label": "seal"}],
    }
    with psycopg.connect(host=database.host, port=database.port, user=database.user, dbname=database.database, autocommit=True) as conn:
        conn.execute(schema.encode())
        # Every later session (the service's own) runs fourteen hours from UTC.
        conn.execute(sql.SQL("ALTER DATABASE {} SET timezone TO 'Pacific/Kiritimati'").format(sql.Identifier(database.database)))
        conn.execute(
            "INSERT INTO projects (project_id, tenant, state, created_at, document) VALUES ('p1', 'acme', 'labeling', now(), %s)", (json.dumps(project),)
        )
        for task in tasks:
            conn.execute(
                "INSERT INTO tasks (task_id, project_id, state, assignee, lease_expires_at, replica_of, document) VALUES (%s, 'p1', %s, NULL, NULL, NULL, %s)",
                (task["task_id"], task["state"], json.dumps(task)),
            )
        conn.execute("INSERT INTO drafts (task_id, revision, document) VALUES ('b-submitted', 1, %s)", (json.dumps(draft),))


def test_a_database_the_previous_release_filled_is_upgraded_twice_and_listed_counted_and_sorted(
    state_database: StateDatabase, state_settings: AnnotatorSettings, state_app: Callable[..., FastAPI]
) -> None:
    _fill_as_the_previous_release(state_database)

    async def allow(**_kw: object) -> bool:
        return True

    app = state_app(router, projects.router, tasks.router)
    app.dependency_overrides[get_checker] = lambda: allow
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: "gina"
    with TestClient(app) as client:
        portal = client.portal
        assert portal is not None, "the client runs no app"
        # The second start: the schema applied again over the columns the first one added.
        portal.call(_open_and_close, state_settings)

        def ids(**params: Any) -> list[str]:
            body = client.get("/projects/p1/tasks", params={"include": "details", **params}).json()
            return [d["task_id"] for d in body["details"]]

        whole = client.get("/projects/p1/tasks", params={"include": "details"}).json()
        by_submitted = ids(sort="submitted", order="desc")
        by_sent = ids(sort="sent")
        seals = ids(label="seal")
        sent_this_year = ids(date="sent", since="2026-01-01T00:00:00Z")
        submitted_that_day = ids(date="submitted", since="2026-11-10T00:00:00Z", until="2026-11-11T00:00:00Z")
        listed = client.get("/projects", params={"tenant": "acme"})
        read = client.get("/projects/p1")
        draft = client.get("/tasks/b-submitted/draft")

    assert (whole["total"], whole["matched"], {k: v for k, v in whole["counts"].items() if v}) == (3, 3, {"unassigned": 1, "in_review": 2})
    assert [d.get("sent_at") for d in whole["details"]] == [None, None, None], "an item the previous release sent records no send instant"
    assert (by_submitted, by_sent, seals, sent_this_year, submitted_that_day) == (
        ["b-submitted", "a-untouched", "c-no-offset"],
        ["a-untouched", "b-submitted", "c-no-offset"],
        ["b-submitted"],
        [],
        ["b-submitted"],
    )
    untouched = next(d for d in whole["details"] if d["task_id"] == "a-untouched")
    assert (len(untouched["source"]["keys"]), len(untouched["source"]["where"])) == (70, 600)
    assert (listed.status_code, read.status_code, draft.status_code) == (200, 200, 200), (listed.text[:200], read.text[:200], draft.text[:200])
    assert [p["ontology"]["classes"][0]["colour"] for p in listed.json()["projects"]] == ["c" * 100]
    assert read.json()["project"]["ontology"]["classes"][0]["colour"] == "c" * 100
    assert [(link["from_shape"], link["to_shape"], len(link["name"])) for link in draft.json()["links"]] == [("s1", LONG_ID, 600)]


async def _open_and_close(settings: AnnotatorSettings) -> None:
    store = await AnnotatorStore.open(settings)
    await store.close()
