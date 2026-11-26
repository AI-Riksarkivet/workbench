"""A database the previous annotator filled upgrades in place at the next start, and the data manager reads it.

The schema is applied at every start (`annotator.state.store.apply_schema`), so an upgrade is that apply over rows an
earlier release wrote: the listing's columns are generated from each document by Postgres, so the rows already there get
them as the columns are added, with no step that rewrites a document. Driven on the real PostgreSQL fixture: each earlier
release's schema (`fixtures/schema_before_the_data_manager.sql`, the release before the data manager, and
`fixtures/schema_at_the_data_manager.sql`, the data manager's own, before a draft said whether it holds work) and documents
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
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import projects, tasks
from annotator.api.v1.endpoints.project_events import router
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import AnnotationProject, ProjectState, Shape, Task, TaskState
from annotator.state.store import SCHEMA_LOCK, AnnotatorStore
from annotator.text import MAX_ID
from tests.postgres import StateDatabase


FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Path(__file__).parents[1] / "src" / "annotator" / "state" / "schema.sql"
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


def _fill_as_the_previous_release(database: StateDatabase, previous: Path) -> None:
    schema = Template(previous.read_text()).substitute(
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
        "shapes": [
            {"shape_id": "s1", "shape_type": "bbox", "label": "seal"},
            {"shape_id": LONG_ID, "shape_type": "bbox", "label": "seal"},
            # Whole-item tags as an earlier release wrote them, one carrying text, one a model's proposal.
            {"shape_id": "t-said", "shape_type": "tag", "label": "seal", "text": "a seal in red wax"},
            {"shape_id": "t-proposed", "shape_type": "tag", "label": "seal", "status": "prediction"},
        ],
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
        # A draft whose every shape was taken away again: it holds no work.
        emptied = {**draft, "task_id": "c-no-offset", "author": "dex~bo", "links": [], "shapes": []}
        conn.execute("INSERT INTO drafts (task_id, revision, document) VALUES ('c-no-offset', 2, %s)", (json.dumps(emptied),))


@pytest.mark.parametrize("previous", ["schema_before_the_data_manager.sql", "schema_at_the_data_manager.sql"])
def test_a_database_the_previous_release_filled_is_upgraded_twice_and_listed_counted_and_sorted(
    previous: str, state_database: StateDatabase, state_settings: AnnotatorSettings, state_app: Callable[..., FastAPI]
) -> None:
    _fill_as_the_previous_release(state_database, FIXTURES / previous)

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

        whole = client.get("/projects/p1/tasks", params={"include": ["details", "work"]}).json()
        by_submitted = ids(sort="submitted", order="desc")
        by_sent = ids(sort="sent")
        seals = ids(label="seal")
        worked = ids(has_work=True)
        sent_this_year = ids(date="sent", since="2026-01-01T00:00:00Z")
        submitted_that_day = ids(date="submitted", since="2026-11-10T00:00:00Z", until="2026-11-11T00:00:00Z")
        listed = client.get("/projects", params={"tenant": "acme"})
        read = client.get("/projects/p1")
        draft = client.get("/tasks/b-submitted/draft")

    assert (whole["total"], whole["matched"], {k: v for k, v in whole["counts"].items() if v}) == (3, 3, {"unassigned": 1, "in_review": 2})
    assert [d.get("sent_at") for d in whole["details"]] == [None, None, None], "an item the previous release sent records no send instant"
    assert (by_submitted, by_sent, seals, worked, sent_this_year, submitted_that_day) == (
        ["b-submitted", "a-untouched", "c-no-offset"],
        ["a-untouched", "b-submitted", "c-no-offset"],
        ["b-submitted"],
        ["b-submitted"],
        [],
        ["b-submitted"],
    )
    assert {d["task_id"]: d["work"] for d in whole["details"]} == {
        "a-untouched": {"revision": 0, "tags": [], "tags_cut": False, "proposals": 0},
        "b-submitted": {
            "revision": 1,
            "tags": [
                {"id": "t-said", "label": "seal", "detailed": True, "prediction": False, "proposed_by": None},
                {"id": "t-proposed", "label": "seal", "detailed": False, "prediction": True, "proposed_by": None},
            ],
            "tags_cut": False,
            "proposals": 1,
        },
        "c-no-offset": {"revision": 2, "tags": [], "tags_cut": False, "proposals": 0},
    }, "each draft the earlier release wrote answers its work from the column the upgrade added"
    untouched = next(d for d in whole["details"] if d["task_id"] == "a-untouched")
    assert (len(untouched["source"]["keys"]), len(untouched["source"]["where"])) == (70, 600)
    assert (listed.status_code, read.status_code, draft.status_code) == (200, 200, 200), (listed.text[:200], read.text[:200], draft.text[:200])
    assert [p["ontology"]["classes"][0]["colour"] for p in listed.json()["projects"]] == ["c" * 100]
    assert read.json()["project"]["ontology"]["classes"][0]["colour"] == "c" * 100
    assert [(link["from_shape"], link["to_shape"], len(link["name"])) for link in draft.json()["links"]] == [("s1", LONG_ID, 600)]


async def _open_and_close(settings: AnnotatorSettings) -> None:
    store = await AnnotatorStore.open(settings)
    await store.close()


def _connect(database: StateDatabase) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(host=database.host, port=database.port, user=database.user, dbname=database.database, autocommit=True)


def _work_tags(database: StateDatabase) -> tuple[Any, int]:
    """The stored `work_tags` of the one draft, and the column's number (a new one each time the column is made again)."""
    with _connect(database) as conn:
        value = conn.execute("SELECT work_tags FROM drafts").fetchone()
        column = conn.execute("SELECT attnum FROM pg_attribute WHERE attrelid = 'drafts'::regclass AND attname = 'work_tags'").fetchone()
    assert value is not None and column is not None
    return value[0], int(column[0])


def _layout(database: StateDatabase) -> tuple[list[Any], list[Any], list[str]]:
    """What a convergence would change: each generated column's number, each table's storage file (a rewrite gives a new
    one) and the functions the columns may call."""
    with _connect(database) as conn:
        columns = conn.execute(
            "SELECT attrelid::regclass::text, attname, attnum FROM pg_attribute "
            "WHERE attrelid IN ('tasks'::regclass, 'drafts'::regclass) AND attgenerated = 's' AND NOT attisdropped ORDER BY 1, 2"
        ).fetchall()
        files = conn.execute("SELECT relname, relfilenode FROM pg_class WHERE relname IN ('tasks', 'drafts') ORDER BY 1").fetchall()
        functions = conn.execute("SELECT proname FROM pg_proc WHERE proname ~ '^annotator_(instant|work_tags)' ORDER BY 1").fetchall()
    return columns, files, [str(f[0]) for f in functions]


def _apply(database: StateDatabase, text: str) -> None:
    """Apply a schema's text as a start does (`store.apply_schema`): one transaction under the schema's lock."""
    schema = Template(text).substitute(project_states=", ".join(f"'{s}'" for s in ProjectState), task_states=", ".join(f"'{s}'" for s in TaskState))
    with _connect(database) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.execute(schema.encode())


async def _with_a_tagged_draft(settings: AnnotatorSettings) -> None:
    """A database the current schema made, holding one draft whose tag carries words."""
    store = await AnnotatorStore.open(settings)
    await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="vasa", state=ProjectState.LABELING))
    await store.send_tasks(
        "p1", [Task.model_validate({"task_id": "t1", "project_id": "p1", "source": {"kind": "chunks", "keys": ["t1"]}, "media": {"kind": "image"}})]
    )
    await store.fire_task("t1", event="claim", actor="ann")
    said = Shape(shape_id="said", shape_type="tag", label="seal", text="in red wax", source="human")
    await store.change_draft("t1", author="ann", change=lambda _current, _now: ([said], []), base_revision=0)
    await store.close()


SAID = {"id": "said", "label": "seal", "detailed": True, "prediction": False, "proposed_by": None}


@pytest.mark.asyncio
async def test_a_column_an_earlier_function_computed_is_computed_again_for_every_row_and_once_only(
    state_settings: AnnotatorSettings, state_database: StateDatabase
) -> None:
    """A row written while a generated column called an earlier function holds what that function computed; the next
    start makes the column again from the current one, so the OLD row answers the current function, drops the earlier
    function, and a start after that finds everything current and changes nothing."""
    await _with_a_tagged_draft(state_settings)
    with _connect(state_database) as conn:
        conn.execute("ALTER TABLE drafts DROP COLUMN work_tags")
        conn.execute("CREATE FUNCTION annotator_work_tags_v0(document jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT AS 'SELECT ''[]''::jsonb'")
        conn.execute("ALTER TABLE drafts ADD COLUMN work_tags jsonb GENERATED ALWAYS AS (annotator_work_tags_v0(document)) STORED")
    stale = _work_tags(state_database)

    await _open_and_close(state_settings)
    converged, layout = _work_tags(state_database), _layout(state_database)
    await _open_and_close(state_settings)

    assert (stale[0], converged[0]) == ([], [SAID])
    assert not any(name == "annotator_work_tags_v0" for name in layout[2]), "the earlier function goes with the column that called it"
    assert (_work_tags(state_database), _layout(state_database)) == (converged, layout), "a start that finds everything current changes nothing"


def _reindented(text: str) -> str:
    return text.replace("\n     SELECT coalesce(jsonb_agg", "\n    SELECT coalesce(jsonb_agg")


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def _spaced(text: str) -> str:
    return text.replace("THEN $$1::timestamptz END$$body$$", "THEN $$1::timestamptz END $$body$$")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit", [pytest.param(_reindented, id="reindented"), pytest.param(_crlf, id="crlf"), pytest.param(_spaced, id="a-space-before-the-end")]
)
async def test_a_cosmetic_edit_of_a_function_body_starts_and_changes_no_column(
    state_settings: AnnotatorSettings, state_database: StateDatabase, edit: Callable[[str], str]
) -> None:
    """A function is named by its body read with its whitespace normalised, so an edit of indentation, line endings or
    spacing keeps every name, and a start over an existing database changes no column and rewrites no table."""
    await _with_a_tagged_draft(state_settings)
    before = _layout(state_database)
    text = SCHEMA.read_text()
    assert edit(text) != text, "the edit names a line of the schema"

    _apply(state_database, edit(text))

    assert _layout(state_database) == before


@pytest.mark.asyncio
async def test_a_real_change_of_a_function_body_computes_the_old_rows_again_and_drops_the_earlier_function(
    state_settings: AnnotatorSettings, state_database: StateDatabase
) -> None:
    await _with_a_tagged_draft(state_settings)
    before = _layout(state_database)
    changed = SCHEMA.read_text().replace("'id', t ->> 'shape_id',", "'id', t ->> 'shape_id', 'seen', true,")

    _apply(state_database, changed)

    after = _layout(state_database)
    assert _work_tags(state_database)[0] == [{**SAID, "seen": True}], "the row written before the change answers the new body"
    assert [f for f in after[2] if f.startswith("annotator_work_tags")] != [f for f in before[2] if f.startswith("annotator_work_tags")]
    assert len([f for f in after[2] if f.startswith("annotator_work_tags")]) == 1, "only the function a column calls is left"
