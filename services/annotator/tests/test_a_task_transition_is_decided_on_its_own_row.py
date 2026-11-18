"""A task's transitions, decided by the state store inside the transaction that holds the task's row.

Driven against the real store on a PostgreSQL database of the test's own, with no HTTP layer in front: every rule
below holds for any caller of the store, which is what makes it true rather than a habit of the endpoints. The
endpoints check the identity-bound rules too, on their own read, to answer a 403 with a reason; these prove the rows
the transaction holds decide, so a lease that moved, a reviewer who became the author, or a project frozen in between
is refused here even when no endpoint looked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
import pytest_asyncio

from annotator.core.config import AnnotatorSettings
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ItemSource, Link, MediaRef, ProjectSettings, ProjectState, Shape, Task, TaskState
from annotator.projects.ontology import LabelOntology
from annotator.state.store import AnnotatorStore
from tests.postgres import StateDatabase


@pytest_asyncio.fixture
async def store(state_settings: AnnotatorSettings) -> AsyncIterator[AnnotatorStore]:
    opened = await AnnotatorStore.open(state_settings)
    yield opened
    await opened.close()


def _task(**kw: Any) -> Task:
    base: dict[str, Any] = {"task_id": "t1", "project_id": "p1", "source": ItemSource(kind="chunks", keys=["doc_1"]), "media": MediaRef(kind="image")}
    base.update(kw)
    return Task(**base)


async def _seed(store: AnnotatorStore, *tasks: Task) -> None:
    """A labeling project whose settings are the first task's, and the tasks sent into it: a send captures what the
    project holds (`AnnotatorStore.send_tasks`)."""
    sent = list(tasks) or [_task()]
    first = sent[0]
    await store.create_project(
        AnnotationProject(
            project_id="p1",
            tenant="acme",
            slug="charters",
            state=ProjectState.LABELING,
            review_required=first.review_required,
            lease_seconds=first.lease_seconds,
            ontology=first.ontology,
        )
    )
    await store.send_tasks("p1", sent)


async def _claimed(store: AnnotatorStore, **kw: Any) -> None:
    await _seed(store, _task(**kw))
    await store.fire_task("t1", event="claim", actor="gina")


async def _read(store: AnnotatorStore, task_id: str = "t1") -> Task:
    task = await store.get_task(task_id)
    assert task is not None, "the store lost the task"
    return task


async def _save(store: AnnotatorStore, shapes: list[dict[str, Any]], **kw: Any) -> Any:
    """Replace the draft of t1 with ``shapes`` (and ``links``), built on revision ``base_revision`` (0: none yet)."""
    drawn = [Shape.model_validate(s) for s in shapes]
    linked = [Link.model_validate(link) for link in kw.pop("links", [])]
    return await store.change_draft(
        "t1",
        author=kw.pop("author", "gina"),
        change=lambda _current, _now: (drawn, linked),
        base_revision=kw.pop("base_revision", 0),
        **kw,
    )


def _remaining(expires: datetime | None) -> float:
    assert expires is not None, "the claim took no lease"
    return (expires - datetime.now(UTC)).total_seconds()


@pytest.mark.asyncio
async def test_a_resent_task_is_left_as_it_is_so_a_redelivered_send_cannot_reset_a_claim(store: AnnotatorStore) -> None:
    await _claimed(store)

    created = await store.send_tasks("p1", [_task()])

    task = await _read(store)
    assert created == []
    assert (task.state, task.assignee) == (TaskState.CLAIMED, "gina"), "re-sending reset a task someone was holding"


@pytest.mark.asyncio
async def test_an_illegal_edge_raises_the_tables_own_refusal(store: AnnotatorStore) -> None:
    """The store consults TASK_EDGES; reviewing an unsubmitted task fails the way `task_transition` fails."""
    await _seed(store)

    with pytest.raises(IllegalTransition, match="illegal task transition: 'accept' is not permitted"):
        await store.fire_task("t1", event="accept", actor="carol")


@pytest.mark.asyncio
async def test_a_principal_cannot_fire_the_edge_a_lapsed_lease_causes(store: AnnotatorStore) -> None:
    """`lease_expired` carries no permission because no principal causes it: a caller of the store naming it is refused,
    and the claim stands."""
    await _claimed(store)

    with pytest.raises(IllegalTransition, match="never by a principal"):
        await store.fire_task("t1", event="lease_expired", actor="henry")

    assert (await _read(store)).assignee == "gina"


@pytest.mark.asyncio
async def test_submit_honours_the_review_required_captured_on_the_task(store: AnnotatorStore) -> None:
    """Captured at send from the project as the send's own transaction holds it, never from the caller nor from a copy
    read before: the item sent (and started) while review was off is accepted on submit; the one sent after the manager
    turned review on goes to review, although the task handed to the send (built, as a send door builds it, from an earlier read) still
    says review is off."""
    await _seed(store, _task(task_id="waived", review_required=False))
    await store.fire_task("waived", event="claim", actor="gina")
    await store.update_settings("p1", ProjectSettings(review_required=True))
    await store.send_tasks("p1", [_task(task_id="required", review_required=False)])
    await store.fire_task("required", event="claim", actor="gina")

    waived = await store.fire_task("waived", event="submit", actor="gina")
    required = await store.fire_task("required", event="submit", actor="gina")

    assert (waived.state, required.state, required.submitted_by) == (TaskState.ACCEPTED, TaskState.IN_REVIEW, "gina")


@pytest.mark.asyncio
async def test_a_send_makes_the_replicas_and_takes_the_ontology_the_project_holds_when_it_lands(store: AnnotatorStore) -> None:
    """The consensus and the ontology are the project's as the send's transaction reads them: an item built while the
    project had no consensus and no classes lands as the three replicas and with the classes the manager set since."""
    await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="charters", state=ProjectState.LABELING))
    await store.update_settings("p1", ProjectSettings(consensus_n=3))
    await store.set_ontology("p1", LabelOntology.model_validate({"classes": [{"name": "seal", "tools": ["bbox"]}]}))

    created = await store.send_tasks("p1", [_task(task_id="g1")])

    sent = await store.get_tasks("p1", created)
    assert [(t.task_id, t.replica_of, [c.name for c in t.ontology.classes]) for t in sent] == [
        ("g1-r1", "g1", ["seal"]),
        ("g1-r2", "g1", ["seal"]),
        ("g1-r3", "g1", ["seal"]),
    ]


@pytest.mark.asyncio
async def test_every_transition_is_appended_to_the_trail(store: AnnotatorStore) -> None:
    await _claimed(store)
    await store.fire_task("t1", event="submit", actor="gina")

    trail = (await _read(store)).transitions

    assert [(t.event, t.from_state, t.to_state, t.by) for t in trail] == [
        ("claim", "unassigned", "claimed", "gina"),
        ("submit", "claimed", "in_review", "gina"),
    ]


# --------------------------------------------------------------------------------------------------
# The lease — a holder and an expiry, renewed by a save and cleared by every edge out of a claim
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_events_door_and_the_canvas_door_renew_the_lease_by_one_rule(store: AnnotatorStore) -> None:
    """A heartbeat (`save_draft` as an event) and a draft save both move the expiry to `lease_seconds` from now."""
    await _seed(store, _task(task_id="t1"), _task(task_id="t2"))
    for task_id in ("t1", "t2"):
        await store.fire_task(task_id, event="claim", actor="gina", lease_seconds=60)

    drawn = [Shape(shape_type="bbox")]
    await store.change_draft("t1", author="gina", change=lambda _current, _now: (drawn, []), base_revision=0, lease_seconds=600)
    await store.fire_task("t2", event="save_draft", actor="gina", lease_seconds=600)

    canvas, events = _remaining((await _read(store, "t1")).lease_expires_at), _remaining((await _read(store, "t2")).lease_expires_at)
    assert 590 <= canvas <= 600 and abs(canvas - events) < 1.0, (canvas, events)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["submit", "release"])
async def test_leaving_claimed_clears_the_lease(event: str, store: AnnotatorStore) -> None:
    """An expiry left on a task that moved on would release it again later."""
    await _claimed(store)

    task = await store.fire_task("t1", event=event, actor="gina")

    assert (task.assignee, task.lease_expires_at) == (None, None)


@pytest.mark.asyncio
async def test_a_lapsed_lease_returns_the_task_and_keeps_the_draft(store: AnnotatorStore, state_database: StateDatabase) -> None:
    """The whole point of a lease: the work is not lost, only the hold on it."""
    await _seed(store, _task(state=TaskState.CLAIMED, assignee="gina", lease_expires_at=datetime.now(UTC) + timedelta(hours=1)))
    await _save(store, [{"shape_type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4}])
    _expire(state_database)

    task = await _read(store)
    draft = await store.get_draft("t1")

    assert (task.state, task.assignee) == (TaskState.UNASSIGNED, None)
    assert task.transitions[-1].event == "lease_expired"
    assert draft is not None and len(draft.shapes) == 1, "a lapsed lease destroyed the draft"


@pytest.mark.asyncio
async def test_claim_defaults_to_the_lease_captured_from_the_project_and_an_explicit_one_wins(store: AnnotatorStore) -> None:
    """`AnnotationProject.lease_seconds` is project config (§4.1), captured on the task at send and read by the claim."""
    await _seed(store, _task(task_id="t1", lease_seconds=600), _task(task_id="t2", lease_seconds=600))

    captured = await store.fire_task("t1", event="claim", actor="gina")
    explicit = await store.fire_task("t2", event="claim", actor="gina", lease_seconds=120)

    assert 590 <= _remaining(captured.lease_expires_at) <= 600, "the claim ignored the captured lease"
    assert 110 <= _remaining(explicit.lease_expires_at) <= 120


# --------------------------------------------------------------------------------------------------
# The draft — one document, revision-guarded
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_is_one_write_for_the_whole_shape_set(store: AnnotatorStore) -> None:
    await _claimed(store)

    out = await _save(store, [{"shape_type": "bbox"}, {"shape_type": "polygon"}])

    stored = await store.get_draft("t1")
    assert out.revision == 1
    assert stored is not None and [s.shape_type for s in stored.shapes] == ["bbox", "polygon"]


@pytest.mark.asyncio
async def test_shape_ids_are_minted_at_save_and_persisted_so_a_republish_is_stable(store: AnnotatorStore) -> None:
    """A shape parsed without an id gets a fresh one, so the publish's replay-determinism rests on the save persisting
    the ids it minted: every read of the draft must answer the same ones."""
    await _claimed(store)
    await _save(store, [{"shape_type": "bbox"}, {"shape_type": "polygon"}])

    first, second = await store.get_draft("t1"), await store.get_draft("t1")

    assert first is not None and second is not None
    ids = [s.shape_id for s in first.shapes]
    assert all(ids) and len(set(ids)) == 2
    assert ids == [s.shape_id for s in second.shapes], "re-reading a draft minted new shape ids"


# --------------------------------------------------------------------------------------------------
# Who an edge names
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_names_the_recipient_not_the_manager_who_fired_it(store: AnnotatorStore) -> None:
    await _seed(store)

    out = await store.fire_task("t1", event="assign", actor="henry", assignee="gina")

    assert (out.assignee, out.transitions[-1].by, out.lease_expires_at) == ("gina", "henry", None), "an assignment pins the task to its recipient"


@pytest.mark.asyncio
async def test_review_notes_are_append_only_across_rounds_and_an_accept_writes_none(store: AnnotatorStore) -> None:
    await _seed(store)
    for message in ("first pass", "second pass"):
        await store.fire_task("t1", event="claim", actor="gina")
        await store.fire_task("t1", event="submit", actor="gina")
        await store.fire_task("t1", event="request_changes", actor="carol", message=message)
    await store.fire_task("t1", event="claim", actor="gina")
    await store.fire_task("t1", event="submit", actor="gina")

    accepted = await store.fire_task("t1", event="accept", actor="carol")

    assert [n.message for n in accepted.review_notes] == ["first pass", "second pass"]


# --------------------------------------------------------------------------------------------------
# The task's ontology — the submit-time output contract, for any caller
# --------------------------------------------------------------------------------------------------


KIE = {
    "kind": "document-question-answering",
    "classes": [{"name": "key", "tools": ["bbox"]}, {"name": "value", "tools": ["bbox"]}],
    "relations": [{"name": "answers", "from_classes": ["key"], "to_classes": ["value"], "required": True}],
}
KIE_SHAPES = [{"shape_id": "s1", "shape_type": "bbox", "label": "key"}, {"shape_id": "s2", "shape_type": "bbox", "label": "value"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ontology", "shapes", "links", "refusal"),
    [
        pytest.param(
            {"kind": "classification", "classes": [{"name": "portrait", "tools": ["tag"]}]},
            [{"shape_type": "bbox", "label": "portrait"}],
            [],
            "portrait",
            id="a-tool-the-class-does-not-allow",
        ),
        pytest.param(
            {"classes": [{"name": "portrait", "tools": ["bbox"], "required": True}]}, [{"shape_type": "bbox", "label": "portrait"}], [], None, id="conforming"
        ),
        pytest.param({}, [{"shape_type": "text", "label": ""}], [], None, id="an-ontology-that-constrains-nothing"),
        pytest.param(
            {"kind": "reading-order", "classes": [{"name": "line", "tools": ["bbox"]}]},
            [],
            [],
            "at least one annotation",
            id="a-constrained-ontology-and-no-draft",
        ),
        pytest.param(KIE, KIE_SHAPES, [{"name": "answers", "from_shape": "s1", "to_shape": "s2"}], None, id="kie-with-its-link"),
        pytest.param(KIE, KIE_SHAPES, [], "answers", id="kie-without-the-required-link"),
        pytest.param(KIE, KIE_SHAPES, [{"name": "answers", "from_shape": "s2", "to_shape": "s1"}], "source", id="a-link-pointing-the-wrong-way"),
    ],
)
async def test_a_submit_is_judged_by_the_ontology_the_task_captured(
    ontology: dict[str, Any], shapes: list[dict[str, Any]], links: list[dict[str, Any]], refusal: str | None, store: AnnotatorStore
) -> None:
    await _claimed(store, ontology=LabelOntology.model_validate(ontology))
    if shapes:
        await _save(store, shapes, links=links)

    if refusal is None:
        assert (await store.fire_task("t1", event="submit", actor="gina")).state is TaskState.IN_REVIEW
        return
    with pytest.raises(IllegalTransition, match=refusal):
        await store.fire_task("t1", event="submit", actor="gina")
    assert (await _read(store)).state is TaskState.CLAIMED, "a refused submit moved the task"


# --------------------------------------------------------------------------------------------------
# Writes the task's current state does not take
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_cannot_be_written_after_the_task_left_claimed(store: AnnotatorStore) -> None:
    """A save that read the task as claimed and arrives after the submit must not put unjudged shapes into reviewed work."""
    await _claimed(store, ontology=LabelOntology.model_validate({"classes": [{"name": "cat", "tools": ["tag"], "required": True}]}))
    await _save(store, [{"shape_type": "tag", "label": "cat"}])
    await store.fire_task("t1", event="submit", actor="gina")

    with pytest.raises(IllegalTransition, match="save_draft"):
        await _save(store, [{"shape_type": "bbox"}] * 3, base_revision=1)

    stored = await store.get_draft("t1")
    assert stored is not None and [s.shape_type for s in stored.shapes] == ["tag"]


@pytest.mark.asyncio
async def test_a_reviewers_fix_lands_in_work_in_review_only_when_verified_and_never_from_the_submitter(store: AnnotatorStore) -> None:
    """In review the draft takes the fix of a reviewer the caller verified (`reviewing`): neither an unverified write
    nor the submitter's own lands."""
    await _claimed(store)
    await _save(store, [{"shape_type": "tag", "label": "cat"}])
    await store.fire_task("t1", event="submit", actor="gina")

    for author, reviewing in (("rita", False), ("gina", True)):
        with pytest.raises(IllegalTransition, match="save_draft"):
            await _save(store, [{"shape_type": "bbox"}], author=author, reviewing=reviewing, base_revision=1)
    saved = await _save(store, [{"shape_type": "bbox"}], author="rita", reviewing=True, base_revision=1)

    assert (saved.author, [s.shape_type for s in saved.shapes]) == ("rita", ["bbox"])


# --------------------------------------------------------------------------------------------------
# The identity-bound rules of §5.2, decided on the rows the transaction holds
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lease_that_moved_refuses_the_previous_holders_submit(store: AnnotatorStore) -> None:
    await _claimed(store)
    await store.fire_task("t1", event="release", actor="gina")
    await store.fire_task("t1", event="claim", actor="dave")

    with pytest.raises(IllegalTransition, match="held by dave"):
        await store.fire_task("t1", event="submit", actor="gina")

    task = await _read(store)
    assert (task.state, task.assignee) == (TaskState.CLAIMED, "dave"), "a stale submit moved the new holder's task"


@pytest.mark.asyncio
async def test_the_reviewer_becoming_the_author_is_caught(store: AnnotatorStore) -> None:
    await _claimed(store)
    await store.fire_task("t1", event="submit", actor="gina")

    with pytest.raises(IllegalTransition, match="own submission"):
        await store.fire_task("t1", event="accept", actor="gina")

    assert (await _read(store)).state is TaskState.IN_REVIEW


@pytest.mark.asyncio
async def test_release_by_a_non_holder_needs_the_managers_answer_stated(store: AnnotatorStore) -> None:
    """`release` is "lease holder OR can_manage", and `can_manage` is the checker's answer, which the store cannot
    compute: unstated is unauthorized."""
    await _claimed(store)

    with pytest.raises(IllegalTransition, match="needs can_manage"):
        await store.fire_task("t1", event="release", actor="henry")
    out = await store.fire_task("t1", event="release", actor="henry", subject_can_manage=True)

    assert (out.state, out.assignee) == (TaskState.UNASSIGNED, None)


@pytest.mark.asyncio
async def test_the_managers_answer_does_not_unlock_the_lease_holder_rules(store: AnnotatorStore) -> None:
    """§5.2 rule 2 is "only the lease holder writes … even a manager"."""
    await _claimed(store)

    with pytest.raises(IllegalTransition, match="held by gina"):
        await store.fire_task("t1", event="submit", actor="dave", subject_can_manage=True)


@pytest.mark.asyncio
async def test_a_draft_from_the_previous_holder_cannot_land_on_the_new_holders_task(store: AnnotatorStore, state_database: StateDatabase) -> None:
    """A save authorized against a lapsed lease would replace the shape set of somebody now working the task."""
    await _seed(store, _task(state=TaskState.CLAIMED, assignee="gina", lease_expires_at=datetime.now(UTC) + timedelta(hours=1)))
    await _save(store, [{"shape_type": "bbox"}])
    _expire(state_database)
    await store.fire_task("t1", event="claim", actor="dave")
    await _save(store, [{"shape_type": "polygon"}] * 3, author="dave", base_revision=1)

    with pytest.raises(IllegalTransition, match="held by dave"):
        await _save(store, [], base_revision=2)

    surviving = await store.get_draft("t1")
    assert surviving is not None and len(surviving.shapes) == 3, "the previous holder's save clobbered the new holder's work"


# --------------------------------------------------------------------------------------------------
# §5.2 rule 5 — nothing escapes a published project, read off the project row the transaction holds
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_frozen_project_refuses_a_transition_and_a_draft(store: AnnotatorStore, state_database: StateDatabase) -> None:
    await _claimed(store)
    _plant(state_database, "UPDATE projects SET state = 'publishing', document = jsonb_set(document, '{state}', '\"publishing\"') WHERE project_id = 'p1'")

    with pytest.raises(IllegalTransition, match="frozen with the published artifact"):
        await store.fire_task("t1", event="submit", actor="gina")
    with pytest.raises(IllegalTransition, match="frozen with the published artifact"):
        await _save(store, [])

    assert ((await _read(store)).state, await store.get_draft("t1")) == (TaskState.CLAIMED, None)


def _expire(database: StateDatabase) -> None:
    """Move the claim's expiry into the past, as the clock does: the lease is read as released from then on."""
    past = datetime.now(UTC) - timedelta(seconds=5)
    with psycopg.connect(host=database.host, port=database.port, user=database.user, dbname=database.database) as conn:
        conn.execute(
            "UPDATE tasks SET lease_expires_at = %s, document = jsonb_set(document, '{lease_expires_at}', to_jsonb(%s::text)) WHERE task_id = 't1'",
            (past, past.isoformat()),
        )


def _plant(database: StateDatabase, statement: str) -> None:
    """Write rows no transition can reach, for the rule that refuses them."""
    with psycopg.connect(host=database.host, port=database.port, user=database.user, dbname=database.database) as conn:
        conn.execute(statement.encode())
