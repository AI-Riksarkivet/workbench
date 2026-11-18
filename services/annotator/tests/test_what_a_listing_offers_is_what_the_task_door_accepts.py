"""What a listing offers a person on an item is exactly what the task door accepts from them, on the real store.

The data manager, its quick view and the canvas's panel offer the actions a task's ``legal_events`` names, and the
listing answers them per caller (`tasks.events_for`, by the one rule `machines.event_refusal` the door refuses by). This
walks the whole table: each role of the model's ladder, against every principal event, on an item in each situation that
changes the answer (nobody's, the caller's claim, another's claim, another's claim whose lease lapsed, the caller's own
submission, another's, sent back, accepted, skipped, a replica of a group the caller worked another replica of, one
another person worked, and an item of a project that is publishing). For every cell, offered ⇒ accepted and accepted ⇒
offered: the event is fired at the door on a fresh copy of the situation and its answer compared with the offer.

An ``assign`` names a recipient who worked nothing: whom it names is judged by the door when named (a recipient who
worked another replica is refused 409, per item, `test_task_endpoints`), never by the offer, which is about the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import project_events
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.machines import TASK_EDGES
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, ProjectState, Task
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase


ME, OTHER, REVIEWER = "me", "other", "rev"
#: Each role of the model's ladder, by the project permissions it holds (`service_kit/governed/auth/model.fga`).
ROLES: dict[str, set[str]] = {
    "viewer": {"can_view"},
    "annotator": {"can_view", "can_claim", "can_annotate", "can_send_items"},
    "reviewer": {"can_view", "can_claim", "can_annotate", "can_send_items", "can_review"},
    "manager": {"can_view", "can_claim", "can_annotate", "can_send_items", "can_review", "can_manage", "can_publish"},
    "owner": {"can_view", "can_claim", "can_annotate", "can_send_items", "can_review", "can_manage", "can_publish", "can_administer"},
}
#: Every event a principal fires.
EVENTS = sorted({event for (_state, event), (_target, permission) in TASK_EDGES.items() if permission is not None})
#: The situations, each one task (`seed` builds them by the store's own transitions).
SITUATIONS = [
    "nobody's",
    "my claim",
    "another's claim",
    "another's lapsed claim",
    "my submission",
    "another's submission",
    "sent back",
    "accepted",
    "skipped",
    "a replica, another of mine",
    "a replica, another's sibling",
    "publishing",
]


def _task(task_id: str, project_id: str, replica_of: str | None = None) -> Task:
    return Task(task_id=task_id, project_id=project_id, source=ItemSource(kind="chunks", keys=[task_id]), media=MediaRef(kind="image"), replica_of=replica_of)


async def seed(store: AnnotatorStore, database: StateDatabase, project_id: str) -> dict[str, str]:
    """One labeling project holding a task in every situation but the frozen one, and a second project, publishing, with
    one item in review; the task id of each situation."""
    ids = {name: f"{project_id}-{i}" for i, name in enumerate(SITUATIONS)}
    ids["a replica, another of mine"] = f"{project_id}m-r2"
    ids["a replica, another's sibling"] = f"{project_id}o-r2"
    frozen = f"{project_id}f"
    ids["publishing"] = f"{frozen}-0"
    await store.create_project(AnnotationProject(project_id=project_id, tenant="acme", slug="cells", state=ProjectState.LABELING))
    await store.create_project(AnnotationProject(project_id=frozen, tenant="acme", slug="frozen", state=ProjectState.LABELING))
    plain = SITUATIONS[:9]
    await store.send_tasks(
        project_id,
        [
            *(_task(ids[name], project_id) for name in plain),
            _task(f"{project_id}m-r1", project_id, f"{project_id}m"),
            _task(ids["a replica, another of mine"], project_id, f"{project_id}m"),
            _task(f"{project_id}o-r1", project_id, f"{project_id}o"),
            _task(ids["a replica, another's sibling"], project_id, f"{project_id}o"),
        ],
    )
    await store.send_tasks(frozen, [_task(ids["publishing"], frozen)])

    async def fire(task_id: str, *steps: tuple[str, str]) -> None:
        for event, actor in steps:
            await store.fire_task(task_id, event=event, actor=actor, message="redo" if event == "request_changes" else "")

    await fire(ids["my claim"], ("claim", ME))
    await fire(ids["another's claim"], ("claim", OTHER))
    await fire(ids["another's lapsed claim"], ("claim", OTHER))
    await fire(ids["my submission"], ("claim", ME), ("submit", ME))
    await fire(ids["another's submission"], ("claim", OTHER), ("submit", OTHER))
    await fire(ids["sent back"], ("claim", OTHER), ("submit", OTHER), ("request_changes", REVIEWER))
    await fire(ids["accepted"], ("claim", OTHER), ("submit", OTHER), ("accept", REVIEWER))
    await fire(ids["skipped"], ("claim", OTHER), ("skip", OTHER))
    await fire(f"{project_id}m-r1", ("claim", ME), ("submit", ME))
    await fire(f"{project_id}o-r1", ("claim", OTHER), ("submit", OTHER))
    await fire(ids["publishing"], ("claim", OTHER), ("submit", OTHER))
    with psycopg.connect(host=database.host, port=database.port, user=database.user, dbname=database.database) as conn:
        lapsed = datetime.now(UTC) - timedelta(minutes=1)
        conn.execute(
            "UPDATE tasks SET lease_expires_at = %s, document = jsonb_set(document, '{lease_expires_at}', to_jsonb(%s::text)) WHERE task_id = %s",
            (lapsed, lapsed.isoformat().replace("+00:00", "Z"), ids["another's lapsed claim"]),
        )
        conn.execute("UPDATE projects SET state = 'publishing', document = jsonb_set(document, '{state}', '\"publishing\"') WHERE project_id = %s", (frozen,))
    return ids


def test_every_offer_is_accepted_and_every_acceptance_was_offered(state_app: Callable[..., FastAPI], state_database: StateDatabase) -> None:
    role = ["viewer"]

    async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        return user == ME and relation in ROLES[role[0]]

    app = state_app(tasks_ep.router, project_events.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[get_authorizer] = lambda: None
    app.dependency_overrides[current_subject] = lambda: ME
    cells: dict[tuple[str, str, str], tuple[bool, bool]] = {}
    with TestClient(app) as client:
        portal = client.portal
        assert portal is not None, "the client runs no app"
        store: AnnotatorStore = app.state.store
        for name in ROLES:
            for event in EVENTS:
                role[0] = name
                project_id = f"p{len(cells)}"
                ids = portal.call(seed, store, state_database, project_id)
                offered: dict[str, set[str]] = {}
                for pid in (project_id, f"{project_id}f"):
                    listing: dict[str, Any] = client.get(f"/projects/{pid}/tasks", params={"include": "details", "limit": 200}).json()
                    offered |= {d["task_id"]: {e["event"] for e in d["legal_events"]} for d in listing["details"]}
                for situation, task_id in ids.items():
                    body: dict[str, Any] = {"event": event}
                    if event == "assign":
                        body["assignee"] = "fresh"
                    if event == "request_changes":
                        body["message"] = "redo"
                    answer = client.post(f"/tasks/{task_id}/events", json=body)
                    assert answer.status_code in (200, 403, 409), (name, situation, event, answer.text)
                    cells[(name, situation, event)] = (event in offered[task_id], answer.status_code == 200)

    disagreeing = {cell: f"offered={o} accepted={a}" for cell, (o, a) in cells.items() if o != a}
    assert (len(cells), disagreeing) == (len(ROLES) * len(SITUATIONS) * len(EVENTS), {})
    # The table is not vacuous: the door accepts something in it, and refuses something it lists.
    assert {accepted for _offered, accepted in cells.values()} == {True, False}
