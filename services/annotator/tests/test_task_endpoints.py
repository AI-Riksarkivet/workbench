"""Task endpoints — the authorization surface of the CVAT/Label-Studio loop.

The task router over the real state store, on a PostgreSQL database of the test's own (`state_app`): which `can_*`
each event demands, on WHICH object, and the rules the transition table cannot express because they depend on the
task's own rows. The checker is the one stand-in, so each test names exactly the relations its caller holds; what a
refused request did NOT do is read back from the store.

The permission is not asserted per-route by hand. `TASK_EDGES` carries it on the edge, so these read
the expectation from the machine — a new edge is gated automatically and a route cannot quietly drift
from the model.

Three of these exist because an adversarial review found the corresponding defect in the first
version of this file's subject:

- the check ran on `project:<tenant>`, a type on which `model.fga` defines none of these relations;
- `review_required` was a request field, so an annotator could submit with review waived;
- system-only edges were exposed unauthenticated, because "no permission required" was read as
  "no permission checked".
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pyarrow as pa
import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_authorizer, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.machines import TASK_EDGES
from annotator.projects.models import AnnotationProject, Draft, ItemSource, MediaRef, ProjectState, Shape, Task, TaskState
from annotator.projects.ontology import LabelOntology
from annotator.state.store import AnnotatorStore
from annotator.text import MAX_ID, MAX_JSON_DEPTH
from tests.postgres import StateDatabase


SUBJECT = "gina"
PROJECT_ID = "proj-1"


class _Door:
    """The task router as one caller holding a set of relations, over the real store."""

    def __init__(self, client: TestClient, app: FastAPI, seen: list[dict[str, Any]], database: StateDatabase) -> None:
        self.client, self._app, self.seen, self._database = client, app, seen, database

    @property
    def store(self) -> AnnotatorStore:
        return self._app.state.store

    @property
    def portal(self) -> BlockingPortal:
        """The app's own event loop, where the store's pool lives; open while the client is."""
        portal = self.client.portal
        assert portal is not None, "the client is not open"
        return portal

    def seed(self, *tasks: Task, consensus_n: int = 1) -> None:
        """A labeling project holding ``tasks`` in the states they are handed, the project's settings the first task's (a
        send captures what the project holds)."""
        project = AnnotationProject(project_id=PROJECT_ID, tenant="acme", slug="charters", state=ProjectState.LABELING, consensus_n=consensus_n)
        if tasks:
            project = project.model_copy(
                update={"review_required": tasks[0].review_required, "lease_seconds": tasks[0].lease_seconds, "ontology": tasks[0].ontology}
            )
        self.portal.call(self.store.create_project, project)
        self.portal.call(self.store.send_tasks, PROJECT_ID, list(tasks))

    def freeze_project_as(self, state: ProjectState) -> None:
        """Plant a frozen project under a task that is not terminal: no edge reaches that, which is the rule under test."""
        with psycopg.connect(host=self._database.host, port=self._database.port, user=self._database.user, dbname=self._database.database) as conn:
            conn.execute(
                "UPDATE projects SET state = %s, document = jsonb_set(document, '{state}', to_jsonb(%s::text)) WHERE project_id = %s",
                (str(state), str(state), PROJECT_ID),
            )

    def task(self, task_id: str = "t1") -> Task:
        found = self.portal.call(self.store.get_task, task_id)
        assert found is not None, f"task {task_id} is gone"
        return found

    def draft(self, task_id: str = "t1") -> Draft | None:
        return self.portal.call(self.store.get_draft, task_id)

    def save_draft(self, shapes: list[Shape], task_id: str = "t1") -> Draft:
        return self.portal.call(
            lambda: self.store.change_draft(task_id, author=SUBJECT, change=lambda _current, _now: (shapes, []), base_revision=None, require_base=False)
        )


@pytest.fixture
def door(state_app: Callable[..., FastAPI], state_database: StateDatabase) -> Iterator[Callable[..., _Door]]:
    """`door(allow=...)`: every relation (True), none (False: a stranger), or the relations the caller holds."""
    opened: list[TestClient] = []

    def build(*, allow: bool | set[str], raise_server_exceptions: bool = True) -> _Door:
        seen: list[dict[str, Any]] = []

        async def checker(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
            seen.append({"user": user, "relation": relation, "obj": obj})
            return allow if isinstance(allow, bool) else relation in allow

        app = state_app(tasks_ep.router)
        app.dependency_overrides[get_checker] = lambda: checker
        app.dependency_overrides[get_authorizer] = lambda: None
        app.dependency_overrides[current_subject] = lambda: SUBJECT
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        client.__enter__()
        opened.append(client)
        return _Door(client, app, seen, state_database)

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def _task(**kw: Any) -> Task:
    """A task as the send door writes one: its item and media, the project's captures, then the state under test."""
    base: dict[str, Any] = {
        "task_id": "t1",
        "project_id": PROJECT_ID,
        "source": ItemSource(kind="chunks", keys=["t1"]),
        "media": MediaRef(kind="image"),
    }
    base.update(kw)
    return Task(**base)


# --------------------------------------------------------------------------------------------------
# The permission — and the object — come from the model, not from the route
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event", "assignee"),
    [(TaskState.UNASSIGNED, "claim", None), (TaskState.UNASSIGNED, "assign", None), (TaskState.CLAIMED, "release", SUBJECT)],
)
def test_every_edge_checks_the_permission_the_table_declares(state: TaskState, event: str, assignee: str | None, door: Callable[..., _Door]) -> None:
    """The edge's own permission, asked of the VERIFIED subject on the task's own project, is what admits it: every other
    permission without it is refused, and it alone (with the view) admits."""
    expected = TASK_EDGES[(state, event)][1]
    others = {permission for _target, permission in TASK_EDGES.values() if permission not in (None, expected)}
    without, alone = door(allow={"can_view", *others}), door(allow={"can_view", str(expected)})
    without.seed(_task(state=state, assignee=assignee))

    refused = without.client.post("/tasks/t1/events", json={"event": event})
    admitted = alone.client.post("/tasks/t1/events", json={"event": event})

    assert (refused.status_code, refused.json()["relation"], admitted.status_code) == (403, expected, 200), admitted.text
    assert {(c["user"], c["obj"]) for c in alone.seen if c["relation"] == expected} == {(SUBJECT, f"project:{PROJECT_ID}")}


# --------------------------------------------------------------------------------------------------
# System-only edges are not a public surface
# --------------------------------------------------------------------------------------------------


def test_a_system_only_edge_is_refused_over_http(door: Callable[..., _Door]) -> None:
    """`lease_expired` has permission `None` because no PRINCIPAL fires it — a lease running out does.
    Treating "no permission required" as "no permission checked" would let anyone strip another annotator's claim."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee="dave"))

    r = d.client.post("/tasks/t1/events", json={"event": "lease_expired"})

    assert r.status_code == 403
    assert (d.task().state, d.task().assignee) == (TaskState.CLAIMED, "dave"), "a system-only edge moved the task over HTTP"


def test_every_permissionless_edge_is_in_the_refused_set() -> None:
    """Derived from the machine, so a new `None`-permission edge is refused without a code change."""
    from annotator.api.v1.endpoints.tasks import SYSTEM_ONLY_EVENTS

    assert {e for (_s, e), (_t, p) in TASK_EDGES.items() if p is None} == SYSTEM_ONLY_EVENTS
    assert "lease_expired" in SYSTEM_ONLY_EVENTS


# --------------------------------------------------------------------------------------------------
# review_required is not a request field
# --------------------------------------------------------------------------------------------------


def test_an_annotator_cannot_waive_review_from_the_request_body(door: Callable[..., _Door]) -> None:
    """`review_required` is captured on the task at send time, so a submitter passing `false` still lands in review
    rather than self-approving past the one guarantee the whole plane exists to provide."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT, review_required=True))

    r = d.client.post("/tasks/t1/events", json={"event": "submit", "review_required": False})

    assert r.status_code == 200, r.text
    assert d.task().state is TaskState.IN_REVIEW, "the request body waived the review the task captured"


def test_a_viewer_without_the_edges_relation_is_403(door: Callable[..., _Door]) -> None:
    d = door(allow={"can_view"})
    d.seed(_task())

    r = d.client.post("/tasks/t1/events", json={"event": "claim"})

    assert r.status_code == 403
    assert d.task().state is TaskState.UNASSIGNED, "the task moved despite the check failing"


def test_an_edge_absent_from_the_table_is_409_to_a_viewer(door: Callable[..., _Door]) -> None:
    d = door(allow={"can_view"})
    d.seed(_task(state=TaskState.UNASSIGNED))

    assert d.client.post("/tasks/t1/events", json={"event": "accept"}).status_code == 409


def test_a_stranger_learns_nothing_of_a_task_whether_it_exists_or_which_state_it_is_in(door: Callable[..., _Door]) -> None:
    """Task ids are guessable (`{group}-r{k}`): to someone who may not view a task, an absent one and a real one in any
    state answer the same bytes, on the read and on an event alike, and no relation or state is named."""
    d = door(allow=False)
    d.seed(
        _task(state=TaskState.UNASSIGNED),
        _task(task_id="t2", state=TaskState.CLAIMED, assignee="dave"),
        _task(task_id="t3", state=TaskState.IN_REVIEW, submitted_by="dave"),
    )
    ids = ["t1", "t2", "t3", "nope"]

    reads = {d.client.get(f"/tasks/{t}").content.replace(t.encode(), b"<id>") for t in ids}
    claims = {d.client.post(f"/tasks/{t}/events", json={"event": "claim"}).content.replace(t.encode(), b"<id>") for t in ids}
    statuses = {d.client.get(f"/tasks/{t}").status_code for t in ids} | {d.client.post(f"/tasks/{t}/events", json={"event": "claim"}).status_code for t in ids}

    assert len(reads | claims) == 1 and statuses == {404}
    assert json.loads(next(iter(reads)))["detail"] == "No task <id> that you can see"
    assert [d.task(t).state for t in ("t1", "t2", "t3")] == [TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.IN_REVIEW]


# --------------------------------------------------------------------------------------------------
# The rules the table cannot express
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["accept", "fix_and_accept", "request_changes"])
def test_a_reviewer_may_not_review_their_own_submission(event: str, door: Callable[..., _Door]) -> None:
    """§5.2. `can_review` is not enough — the submitter is excluded even when they hold it."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.IN_REVIEW, submitted_by=SUBJECT))

    r = d.client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert "own submission" in r.text
    assert d.task().state is TaskState.IN_REVIEW


def test_someone_elses_submission_can_be_reviewed(door: Callable[..., _Door]) -> None:
    d = door(allow=True)
    d.seed(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))

    r = d.client.post("/tasks/t1/events", json={"event": "accept"})

    assert r.status_code == 200, r.text
    assert (d.task().state, d.task().reviewed_by) == (TaskState.ACCEPTED, SUBJECT)


# --------------------------------------------------------------------------------------------------
# §5.2 rule 5 — nothing escapes a published project
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("frozen", [ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED])
def test_no_task_transition_survives_a_published_project(frozen: ProjectState, door: Callable[..., _Door]) -> None:
    """Provenance is frozen with the artifact. A task that moved after the publish would describe a
    dataset that does not match it — and during `publishing` it could change the very rows the
    saga is reading."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    d.freeze_project_as(frozen)

    r = d.client.post("/tasks/t1/events", json={"event": "accept"})

    assert r.status_code == 409, r.text
    assert "frozen with the published artifact" in r.text
    assert d.task().state is TaskState.IN_REVIEW


# --------------------------------------------------------------------------------------------------
# §5.2 rule 2 — only the lease holder writes, "even a manager"
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["submit", "skip"])
def test_skip_and_submit_are_lease_holder_only(event: str, door: Callable[..., _Door]) -> None:
    """A passer-by skipping would discard work someone else is holding."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee="dave"))

    r = d.client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert (r.json()["detail"], r.json()["holder"]) == ("another person holds this item", "dave")
    assert (d.task().state, d.task().assignee) == (TaskState.CLAIMED, "dave")


def test_release_is_refused_to_a_non_holder_without_can_manage(door: Callable[..., _Door]) -> None:
    """§5.2: `release` is "lease holder OR can_manage"; plain `can_annotate` breaks no other annotator's claim."""
    d = door(allow={"can_view", "can_annotate"})
    d.seed(_task(state=TaskState.CLAIMED, assignee="dave"))

    r = d.client.post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 403
    assert "needs can_manage" in r.text
    assert any(c["relation"] == "can_manage" for c in d.seen), "the manager escape hatch was never consulted"
    assert d.task().assignee == "dave"


def test_a_manager_may_release_a_task_held_by_someone_else(door: Callable[..., _Door]) -> None:
    """The documented escape hatch for a task pinned to someone unavailable."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee="dave"))

    r = d.client.post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 200, r.text
    assert (d.task().state, d.task().assignee) == (TaskState.UNASSIGNED, None)


# --------------------------------------------------------------------------------------------------
# assign names a recipient
# --------------------------------------------------------------------------------------------------


def test_a_manager_assigns_to_a_named_user(door: Callable[..., _Door]) -> None:
    """`assign` is the manager-driven distribution mechanism: the recipient holds the task, the manager is the actor."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.UNASSIGNED))

    r = d.client.post("/tasks/t1/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 200, r.text
    task = d.task()
    assert (task.assignee, task.transitions[-1].by) == ("dave", SUBJECT)


# --------------------------------------------------------------------------------------------------
# Consensus v1 — one replica per annotator per group
# --------------------------------------------------------------------------------------------------


def test_claiming_a_second_replica_of_the_same_group_is_409(door: Callable[..., _Door]) -> None:
    """Independence is the point of consensus: the same person labeling two replicas of one item is
    one opinion counted twice. Refused with the rule and the replica already held NAMED."""
    d = door(allow=True)
    d.seed(
        _task(task_id="g1-r1", state=TaskState.CLAIMED, assignee=SUBJECT, replica_of="g1"),
        _task(task_id="g1-r2", replica_of="g1"),
        consensus_n=2,
    )

    r = d.client.post("/tasks/g1-r2/events", json={"event": "claim"})

    assert r.status_code == 409, r.text
    assert (r.json()["detail"], r.json()["replica"]) == ("one replica per annotator per group: you hold, held or worked another replica of this item", "g1-r1")
    assert d.task("g1-r2").state is TaskState.UNASSIGNED


def test_assigning_a_second_replica_to_the_same_recipient_is_409(door: Callable[..., _Door]) -> None:
    """The assign edge is guarded on the RECIPIENT, and a sibling already WORKED (submitted, lease long released)
    counts as much as one held."""
    d = door(allow=True)
    d.seed(
        _task(task_id="g1-r1", state=TaskState.IN_REVIEW, submitted_by="dave", replica_of="g1"),
        _task(task_id="g1-r2", replica_of="g1"),
        consensus_n=2,
    )

    r = d.client.post("/tasks/g1-r2/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 409, r.text
    assert {k: r.json()[k] for k in ("detail", "principal", "replica")} == {
        "detail": "one replica per annotator per group: they hold, held or worked another replica of this item",
        "principal": "dave",
        "replica": "g1-r1",
    }
    assert d.task("g1-r2").assignee is None


def test_a_replica_once_held_and_released_still_bars_its_siblings(door: Callable[..., _Door]) -> None:
    """Releasing a replica clears its holder but not what its holder saw: a person who held one replica of a group is
    refused every sibling, or a release-and-claim hop would read another annotator's work and come back."""
    d = door(allow=True)
    d.seed(_task(task_id="g1-r1", replica_of="g1"), _task(task_id="g1-r2", replica_of="g1"), consensus_n=2)
    assert d.client.post("/tasks/g1-r1/events", json={"event": "claim"}).status_code == 200
    assert d.client.post("/tasks/g1-r1/events", json={"event": "release"}).status_code == 200

    r = d.client.post("/tasks/g1-r2/events", json={"event": "claim"})

    assert r.status_code == 409, r.text
    assert r.json()["replica"] == "g1-r1", "the refusal must name the replica once held"
    assert d.task("g1-r2").state is TaskState.UNASSIGNED


# --------------------------------------------------------------------------------------------------
# Import (#39) — Arrow IPC into the draft
# --------------------------------------------------------------------------------------------------


def _ipc(rows: list[dict[str, Any]]) -> bytes:
    import io

    table = pa.Table.from_pylist(rows)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def test_an_import_appends_to_the_draft_rather_than_replacing_it(door: Callable[..., _Door]) -> None:
    """An import onto a task somebody had already worked must not destroy that work: appending is never destructive,
    and a duplicate is removable on the canvas."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    d.save_draft([Shape(shape_id="drawn-1", shape_type="bbox")])

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox", "label": "figure"}]))

    assert r.status_code == 200, r.text
    stored = d.draft()
    assert stored is not None and [s.shape_id for s in stored.shapes] == ["drawn-1", "a1"], "the hand-drawn shape was destroyed by the import"
    assert (r.json()["imported"], stored.revision, stored.origin) == (1, 2, "import")


def test_the_tasks_own_ontology_refuses_a_foreign_label(door: Callable[..., _Door]) -> None:
    """The rules come from the task, never from the request — a caller must not supply the contract it is judged by."""
    d = door(allow=True)
    ontology = LabelOntology.model_validate({"kind": "detection", "classes": [{"name": "figure"}], "allow_empty": True})
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT, ontology=ontology))

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox", "label": "spaceship"}]))

    # 400, not 422: every refusal here means the same thing to the caller ("fix the file and retry") and names its
    # offender in words.
    assert r.status_code == 400, r.text
    assert "spaceship" in r.text
    assert d.draft() is None, "a refused import still wrote to the draft"


_IMPORTED_BOX = {"shape_type": "bbox", "id": "s1", "x": 1.0, "y": 1.0, "width": 2.0, "height": 2.0}


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param({"attributes": json.dumps({"k": "\x00"})}, id="a-nul-in-an-attribute-value"),
        pytest.param({"attributes": json.dumps({"k": "\ud800"})}, id="a-lone-surrogate-in-an-attribute-value"),
        pytest.param({"attributes": json.dumps({"\x00": "v"})}, id="a-nul-in-an-attribute-key"),
        pytest.param({"links": json.dumps([{"name": "r\x00", "to_shape": "s1"}])}, id="a-nul-in-a-link-name"),
        pytest.param({"x": float("nan")}, id="a-number-json-cannot-spell"),
    ],
)
def test_an_import_the_store_cannot_hold_is_a_422_and_writes_nothing(cells: dict[str, Any], door: Callable[..., _Door]) -> None:
    """A cell's JSON is decoded after the request is read, so what it decodes to is judged where the draft is written:
    one refusal for the whole import, naming the task's work, and no draft."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=_ipc([{**_IMPORTED_BOX, **cells}]))

    assert (r.status_code, r.json()["type"]) == (422, "about:blank#unstorable-document"), r.text[:300]
    assert d.draft() is None


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param({"x": "left"}, id="a-coordinate"),
        pytest.param({"char_start": "first"}, id="an-offset"),
        pytest.param({"polygon": ["one", "two"]}, id="a-polygon-point"),
    ],
)
def test_an_import_row_whose_number_is_not_one_is_a_400_naming_the_row(cells: dict[str, Any], door: Callable[..., _Door]) -> None:
    """A row's cell the caller must fix is refused like every other bad row of an import: a 400 naming the row, and no
    draft written."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox", **cells}]))

    assert (r.status_code, r.json()["type"]) == (400, "about:blank#validationerror"), r.text[:300]
    assert "row 0" in r.json()["detail"]
    assert d.draft() is None


def test_an_import_takes_another_tools_long_ids_and_refuses_by_row_what_it_cannot_take(door: Callable[..., _Door]) -> None:
    """An import carries ids another tool minted: a 3000-character id and a link to it are kept as they are. A link to a
    shape the import and the draft do not hold is refused by name, an id past the one bound every shape-id door shares
    (`annotator.text.MAX_ID`) is refused naming it, and an attributes or links cell nesting past the depth the annotator
    decodes (`annotator.text.MAX_JSON_DEPTH`) is refused naming its row, whatever the interpreter's recursion limit."""
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    long_id = "y" * 3000
    rows = [{**_IMPORTED_BOX, "id": long_id, "links": None}, {**_IMPORTED_BOX, "id": "s2", "links": json.dumps([{"name": "r", "to_shape": long_id}])}]
    deep = "[" * 10_000 + "]" * 10_000

    unknown = d.client.post("/tasks/t1/import", content=_ipc([{**_IMPORTED_BOX, "links": json.dumps([{"name": "r", "to_shape": "x" * 2000}])}]))
    too_long = d.client.post("/tasks/t1/import", content=_ipc([{**_IMPORTED_BOX, "links": json.dumps([{"name": "r", "to_shape": "x" * (MAX_ID + 1)}])}]))
    deep_attributes = d.client.post("/tasks/t1/import", content=_ipc([{**_IMPORTED_BOX, "attributes": '{"k":' + deep + "}"}]))
    deep_links = d.client.post("/tasks/t1/import", content=_ipc([{**_IMPORTED_BOX, "links": deep}]))
    kept = d.client.post("/tasks/t1/import", content=_ipc(rows))

    assert (unknown.status_code, "points at unknown shape" in unknown.json()["detail"]) == (400, True), unknown.text[:300]
    assert (too_long.status_code, too_long.json()["detail"]) == (400, f"import refused — row 0 has a link end longer than {MAX_ID} characters")
    assert [(r.status_code, r.json()["detail"]) for r in (deep_attributes, deep_links)] == [
        (400, f"import refused — row 0's attributes cell nests deeper than {MAX_JSON_DEPTH} levels"),
        (400, f"import refused — row 0's links cell nests deeper than {MAX_JSON_DEPTH} levels"),
    ]
    assert kept.status_code == 200, kept.text[:300]
    stored = d.draft()
    assert stored is not None
    assert ([s.shape_id for s in stored.shapes], [(link.from_shape, link.to_shape) for link in stored.links]) == ([long_id, "s2"], [("s2", long_id)])


@pytest.mark.parametrize("state", [TaskState.ACCEPTED])
def test_annotations_can_only_be_imported_into_a_claimed_task(state: TaskState, door: Callable[..., _Door]) -> None:
    """Importing is annotating: an accepted task is never rewritten after review, as Arrow or otherwise."""
    d = door(allow=True)
    d.seed(_task(state=state, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 409, r.text
    assert d.draft() is None


def test_an_import_by_a_non_holder_is_403(door: Callable[..., _Door]) -> None:
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee="omar"))

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 403, r.text
    assert d.draft() is None


def test_an_import_requires_can_annotate(door: Callable[..., _Door]) -> None:
    d = door(allow={"can_view"})
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 403, r.text
    assert d.seen[1]["relation"] == "can_annotate"
    assert d.draft() is None


def _rewritten(table: pa.Table, good: bytes, bad: bytes, *, framing: str = "stream") -> bytes:
    """`table` as an import body whose IPC framing still parses after `good` is overwritten with `bad`."""
    sink = pa.BufferOutputStream()
    opener = pa.ipc.new_stream if framing == "stream" else pa.ipc.new_file
    with opener(sink, table.schema) as writer:
        writer.write_table(table)
    body = sink.getvalue().to_pybytes()
    assert body.count(good) == 1, "the bytes to tamper with are not unique in the body"
    return body.replace(good, bad)


def _tampered(kind: pa.DataType, good: bytes, bad: bytes, *, framing: str = "stream") -> bytes:
    """Two shapes whose `text` column's buffers are rewritten."""
    values: list[Any] = [b"hello", b"world"] if kind == pa.binary() else ["hello", "world"]
    return _rewritten(pa.table({"shape_type": ["bbox", "bbox"], "text": pa.array(values, kind)}), good, bad, framing=framing)


_TEXT_OFFSETS = struct.pack("<iii", 0, 5, 10)
_NOT_UTF8 = b"\xff\xfe\xfd\xfc"
_END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"


def _stream(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _messages(body: bytes) -> list[bytes]:
    return [message.serialize().to_pybytes() for message in pa.ipc.MessageReader.open_stream(body)]


def _a_dictionary_batch_whose_id_names_no_field() -> bytes:
    """A schema with one dictionary column, followed by the dictionary batch another stream wrote for its id 1."""
    one = _messages(_stream(pa.table({"shape_type": ["bbox"], "label": pa.array(["cat"]).dictionary_encode()})))
    two = _messages(_stream(pa.table({"a": pa.array(["cat"]).dictionary_encode(), "b": pa.array(["dog"]).dictionary_encode()})))
    return b"".join([one[0], two[2], *one[1:], _END_OF_STREAM])


def _a_compressed_text_declaring(declared: int, codec: str) -> bytes:
    """The `text` values buffer's 8-byte uncompressed-length prefix rewritten, so the reader allocates `declared`."""
    table = pa.table({"shape_type": ["bbox"], "text": ["x" * 1003]})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema, options=pa.ipc.IpcWriteOptions(compression=codec)) as writer:
        writer.write_table(table)
    body = sink.getvalue().to_pybytes()
    at = body.rindex(struct.pack("<q", 1003))
    return body[:at] + struct.pack("<q", declared) + body[at + 8 :]


def _an_integer_narrower_than_eight_bits() -> bytes:
    """An int16 column whose schema declares a 4-bit width, at the byte where int16 and int32 schemas differ."""
    narrow, wide = (_stream(pa.table({"shape_type": ["bbox"], "char_start": pa.array([1], kind)})) for kind in (pa.int16(), pa.int32()))
    at = next(i for i, (a, b) in enumerate(zip(narrow, wide, strict=True)) if a != b)
    return narrow[:at] + bytes([4]) + narrow[at + 1 :]


#: Two batches of shapes whose `text` offsets differ, so the second batch's can be tampered with alone.
_TWO_BATCHES = pa.Table.from_batches(
    [
        pa.record_batch({"id": ["a1", "a2"], "shape_type": ["bbox", "bbox"], "text": ["hello", "world"]}),
        pa.record_batch({"id": ["a3", "a4"], "shape_type": ["bbox", "bbox"], "text": ["abc", "defghij"]}),
    ]
)


TAMPERED_BODIES = [
    pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
    pytest.param(_ipc([{"id": "a1", "shape_type": "bbox", "text": "hello"}])[:-20], id="a-stream-cut-inside-its-batch"),
    pytest.param(_tampered(pa.binary(), _TEXT_OFFSETS, struct.pack("<iii", 0, 5, 4096)), id="binary-offsets-4096-past-the-values-buffer"),
    pytest.param(
        _tampered(pa.binary(), _TEXT_OFFSETS, struct.pack("<iii", 0, 5, 65536), framing="file"), id="file-framed-binary-offsets-past-the-values-buffer"
    ),
    pytest.param(_rewritten(pa.table({"shape_type": ["bbox"], "extra": [{"zzzq": "x"}]}), b"zzzq", _NOT_UTF8), id="a-nested-field-name-that-is-not-utf8"),
    pytest.param(
        _rewritten(pa.table({"shape_type": ["bbox"]}).replace_schema_metadata({"kkkq": "v"}), b"kkkq", _NOT_UTF8), id="a-schema-metadata-key-that-is-not-utf8"
    ),
    pytest.param(
        _rewritten(pa.table({"shape_type": ["bbox"]}, schema=pa.schema([pa.field("shape_type", pa.string(), metadata={"k": "vvvq"})])), b"vvvq", _NOT_UTF8),
        id="a-field-metadata-value-that-is-not-utf8",
    ),
    pytest.param(_a_dictionary_batch_whose_id_names_no_field(), id="a-dictionary-batch-whose-id-names-no-field"),
    pytest.param(_a_compressed_text_declaring(2**50, "zstd"), id="a-zstd-buffer-declaring-2^50-bytes"),
    pytest.param(_an_integer_narrower_than_eight_bits(), id="an-integer-narrower-than-8-bits"),
    pytest.param(_stream(pa.table({"shape_type": ["bbox"]})) + _stream(pa.table({"shape_type": ["bbox"]})), id="two-streams-in-one-body"),
    pytest.param(_stream(_TWO_BATCHES)[:-20], id="a-stream-cut-inside-its-second-batch"),
]


@pytest.mark.parametrize("body", TAMPERED_BODIES)
def test_a_tampered_arrow_body_is_refused_and_nothing_is_imported(body: bytes, door: Callable[..., _Door]) -> None:
    """Framing that parses says nothing about the buffers or names inside it.

    `to_pylist` follows the body's own offsets: one past its values buffer becomes a shape's text
    holding process memory, which the draft stores and the route returns, and one that decreases
    aborts the process. Every such body answers the import's 400, and the draft is never written.
    """
    d = door(allow=True, raise_server_exceptions=False)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=body)

    assert r.status_code == 400, r.text
    assert "not valid Arrow IPC" in r.text, f"refused by something other than the decoder: {r.text}"
    assert d.draft() is None, "a refused import still wrote to the draft"


def test_every_batch_of_an_import_is_imported(door: Callable[..., _Door]) -> None:
    d = door(allow=True)
    d.seed(_task(state=TaskState.CLAIMED, assignee=SUBJECT))

    r = d.client.post("/tasks/t1/import", content=_stream(_TWO_BATCHES))

    assert r.status_code == 200, r.text
    stored = d.draft()
    assert stored is not None
    assert [(s.shape_id, s.text) for s in stored.shapes] == [("a1", "hello"), ("a2", "world"), ("a3", "abc"), ("a4", "defghij")]
