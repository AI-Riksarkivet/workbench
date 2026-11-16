"""A project's rules, held by the state store on a real PostgreSQL database of the test's own.

Every rule here is checked inside the transaction that holds the project row, on the task rows of the same
database, so it holds for any caller: the doors, the publish saga, a retry after a crash. The question most of
them answer is whether unreviewed work can reach a publish.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import psycopg
import pytest
import pytest_asyncio

from annotator.core.config import AnnotatorSettings
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ProjectState, PublishRecord, Shape, Task, TaskState
from annotator.state.store import AnnotatorStore
from service_kit.exceptions import ConflictError


TARGET = "acme$published"


@pytest_asyncio.fixture
async def store(state_settings: AnnotatorSettings) -> AsyncGenerator[AnnotatorStore]:
    opened = await AnnotatorStore.open(state_settings)
    yield opened
    await opened.close()


def _task(task_id: str, project_id: str, **fields: object) -> Task:
    return Task.model_validate(
        {"task_id": task_id, "project_id": project_id, "source": {"kind": "chunks", "keys": [task_id]}, "media": {"kind": "image"}, **fields}
    )


async def _project(store: AnnotatorStore, state: ProjectState = ProjectState.LABELING, **fields: object) -> str:
    project = AnnotationProject.model_validate({"tenant": "acme", "slug": "charters", "state": state, **fields})
    await store.create_project(project)
    return project.project_id


async def _drive(store: AnnotatorStore, task_id: str, state: TaskState, annotator: str = "gina") -> None:
    """Take an unassigned task to ``state`` through the transitions a person would fire."""
    if state is TaskState.UNASSIGNED:
        return
    await store.fire_task(task_id, event="claim", actor=annotator)
    if state is TaskState.CLAIMED:
        return
    if state is TaskState.SKIPPED:
        await store.fire_task(task_id, event="skip", actor=annotator)
        return
    await store.fire_task(task_id, event="submit", actor=annotator)
    if state is TaskState.ACCEPTED:
        await store.fire_task(task_id, event="accept", actor="carol")
    elif state is TaskState.CHANGES_REQUESTED:
        await store.fire_task(task_id, event="request_changes", actor="carol")


async def _with_tasks(store: AnnotatorStore, *states: TaskState) -> str:
    """A frozen project holding one task per state given."""
    project_id = await _project(store)
    await store.send_tasks(project_id, [_task(f"t{i}", project_id) for i in range(len(states))])
    for i, state in enumerate(states):
        await _drive(store, f"t{i}", state)
    await store.fire_project(project_id, event="freeze", actor="meg")
    return project_id


# --------------------------------------------------------------------------------------------------
# Counts and the publish precondition, read from the task rows
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_projects_counts_are_read_from_its_task_rows(store: AnnotatorStore) -> None:
    project_id = await _with_tasks(store, TaskState.ACCEPTED, TaskState.IN_REVIEW, TaskState.ACCEPTED)

    project = await store.get_project(project_id)

    assert project is not None
    assert project.counts == {**dict.fromkeys(TaskState, 0), TaskState.ACCEPTED: 2, TaskState.IN_REVIEW: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.IN_REVIEW, TaskState.CHANGES_REQUESTED])
async def test_one_non_terminal_task_blocks_the_whole_publish(store: AnnotatorStore, blocking: TaskState) -> None:
    project_id = await _with_tasks(store, TaskState.ACCEPTED, TaskState.ACCEPTED, blocking)

    with pytest.raises(IllegalTransition, match="not all terminal"):
        await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)

    assert (await store.task_index(project_id)).may_publish is False
    project = await store.get_project(project_id)
    assert project is not None and (project.state, project.pending_publish_id) == (ProjectState.FROZEN, None)


@pytest.mark.asyncio
async def test_all_terminal_permits_the_publish_and_mints_its_token_once(store: AnnotatorStore) -> None:
    """`skipped` is terminal too. The token, its instant and the target are minted together at the transition."""
    project_id = await _with_tasks(store, TaskState.ACCEPTED, TaskState.SKIPPED)

    published = await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)

    assert published.state is ProjectState.PUBLISHING
    assert (published.pending_target_namespace, published.pending_publish_by) == (TARGET, "henry")
    assert published.pending_publish_id and published.pending_publish_at is not None


@pytest.mark.asyncio
async def test_an_empty_project_cannot_be_published(store: AnnotatorStore) -> None:
    """Every task of no tasks is terminal, so the emptiness check is its own."""
    project_id = await _with_tasks(store)

    with pytest.raises(IllegalTransition, match="no tasks"):
        await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)


@pytest.mark.asyncio
async def test_a_failed_publish_rests_and_its_retry_reuses_the_token_and_the_pinned_target(store: AnnotatorStore) -> None:
    """The table id derives from the token and the target, so a retry pointed elsewhere would make a second table."""
    project_id = await _with_tasks(store, TaskState.ACCEPTED)
    first = await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)
    failed = await store.fire_project(project_id, event="publish_failed", actor="henry", error="the target refused the write")

    with pytest.raises(IllegalTransition, match="pending publish targets"):
        await store.fire_project(project_id, event="publish", actor="henry", target_namespace="acme$gold")
    retried = await store.fire_project(project_id, event="publish", actor="maria", target_namespace=TARGET)

    assert (failed.state, failed.publish_error) == (ProjectState.PUBLISH_FAILED, "the target refused the write")
    assert (retried.state, retried.publish_error, retried.pending_publish_by) == (ProjectState.PUBLISHING, None, "maria")
    assert (retried.pending_publish_id, retried.pending_publish_at) == (first.pending_publish_id, first.pending_publish_at)


@pytest.mark.asyncio
async def test_the_publish_record_is_written_once_so_a_replay_cannot_contradict_it(store: AnnotatorStore) -> None:
    project_id = await _with_tasks(store, TaskState.ACCEPTED)
    await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)
    record = PublishRecord(
        table_id="acme$published$charters", namespace=TARGET, version=7, publish_id="pub-1", published_at=datetime(2026, 7, 28, tzinfo=UTC), published_by="gina"
    )

    await store.record_publish(project_id, record)
    second = await store.record_publish(project_id, record.model_copy(update={"version": 99, "published_by": "someone-else"}))

    assert (second.version, second.published_by) == (7, "gina")


@pytest.mark.asyncio
async def test_progress_notes_are_recorded_only_while_publishing(store: AnnotatorStore) -> None:
    """A note from a run that has since finished must not scribble on its result."""
    project_id = await _with_tasks(store, TaskState.ACCEPTED)
    await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)
    await store.note_progress(project_id, "creating table")
    noted = await store.get_project(project_id)
    await store.fire_project(project_id, event="publish_succeeded", actor="henry")

    await store.note_progress(project_id, "stale step")

    after = await store.get_project(project_id)
    assert noted is not None and noted.publish_progress == "creating table"
    assert after is not None and after.publish_progress is None


# --------------------------------------------------------------------------------------------------
# Sending tasks
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_re_send_creates_nothing_and_leaves_a_held_claim_alone(store: AnnotatorStore) -> None:
    project_id = await _project(store)
    await store.send_tasks(project_id, [_task("t0", project_id)])
    await store.fire_task("t0", event="claim", actor="gina")

    created = await store.send_tasks(project_id, [_task("t0", project_id), _task("t1", project_id)])

    held = await store.get_task("t0")
    assert created == ["t1"]
    assert held is not None and (held.state, held.assignee) == (TaskState.CLAIMED, "gina")


@pytest.mark.asyncio
async def test_a_send_naming_another_projects_task_writes_none_of_the_batch(store: AnnotatorStore) -> None:
    """A task owned by another project could never become terminal here, so the whole send is refused, naming it."""
    other = await _project(store)
    await store.send_tasks(other, [_task("theirs", other)])
    project_id = await _project(store)

    with pytest.raises(ConflictError, match=f"task theirs already belongs to project {other}"):
        await store.send_tasks(project_id, [_task("mine", project_id), _task("theirs", project_id)])

    assert (await store.task_index(project_id)).total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE tasks SET state = 'bogus' WHERE task_id = 't0'", id="task-state"),
        pytest.param("UPDATE projects SET state = 'bogus'", id="project-state"),
        pytest.param(
            "INSERT INTO replica_groups (replica_of, project_id) SELECT 't', project_id FROM tasks WHERE task_id = 't0';"
            "UPDATE tasks SET replica_of = 't' WHERE task_id = 't0'",
            id="replica-shape",
        ),
    ],
)
async def test_the_database_refuses_a_row_the_models_could_not_hold(state_settings: AnnotatorSettings, store: AnnotatorStore, statement: str) -> None:
    """The state columns hold only the enums' values and a replica only its group's `{group}-r{k}` id, checked by the
    database, so a writer outside the store (an operator, a replica on other code) cannot plant one."""
    project_id = await _project(store)
    await store.send_tasks(project_id, [_task("t0", project_id)])
    conninfo = f"host={state_settings.state_host} port={state_settings.state_port} user={state_settings.state_user} dbname={state_settings.state_database}"

    async with await psycopg.AsyncConnection.connect(conninfo) as conn:
        with pytest.raises(psycopg.errors.IntegrityError):
            await conn.execute(statement.encode())


@pytest.mark.asyncio
async def test_two_sends_naming_the_same_tasks_in_opposite_orders_both_succeed(store: AnnotatorStore) -> None:
    """Each send writes its rows in id order, whatever order its items came in, so two overlapping sends wait on one
    another rather than each holding a row the other needs."""
    project_id = await _project(store)
    ids = [f"t{i:03d}" for i in range(200)]

    ascending, descending = await asyncio.gather(
        store.send_tasks(project_id, [_task(t, project_id) for t in ids]),
        store.send_tasks(project_id, [_task(t, project_id) for t in reversed(ids)]),
    )

    assert sorted(ascending + descending) == ids
    assert (await store.task_index(project_id)).total == 200


@pytest.mark.asyncio
async def test_a_consensus_group_stays_in_the_project_that_sent_it(store: AnnotatorStore) -> None:
    """A group's replicas are found and locked by group within their project; a replica of a group another project
    holds is refused, so a group never spans two projects."""
    other = await _project(store)
    await store.send_tasks(other, [_task("g1-r1", other, replica_of="g1")])
    project_id = await _project(store)

    with pytest.raises(ConflictError, match=f"consensus group g1 already belongs to project {other}"):
        await store.send_tasks(project_id, [_task("g1-r2", project_id, replica_of="g1")])

    assert (await store.task_index(project_id)).total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ProjectState.FROZEN, ProjectState.PUBLISHED])
async def test_a_send_into_a_project_past_labeling_is_refused_and_writes_nothing(store: AnnotatorStore, state: ProjectState) -> None:
    project_id = await _project(store, state)

    with pytest.raises(IllegalTransition, match="send"):
        await store.send_tasks(project_id, [_task("t0", project_id)])

    assert (await store.task_index(project_id)).total == 0


# --------------------------------------------------------------------------------------------------
# Dropping a task
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dropped_task_goes_with_its_draft_and_no_longer_blocks_the_publish(store: AnnotatorStore) -> None:
    project_id = await _project(store)
    await store.send_tasks(project_id, [_task("t0", project_id), _task("t1", project_id)])
    await _drive(store, "t0", TaskState.ACCEPTED)
    await store.fire_task("t1", event="claim", actor="gina")
    await store.save_draft("t1", author="gina", shapes=[Shape(shape_type="tag", label="x")], links=[], base_revision=None)

    dropped = await store.drop_task(project_id, "t1")
    again = await store.drop_task(project_id, "t1")
    await store.fire_project(project_id, event="freeze", actor="meg")
    published = await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)

    assert (dropped.removed, dropped.total, again.removed) == (True, 1, False)
    assert (await store.get_task("t1"), await store.get_draft("t1")) == (None, None)
    assert published.state is ProjectState.PUBLISHING


# --------------------------------------------------------------------------------------------------
# Consensus v1 — adjudication, the manager's pick
# --------------------------------------------------------------------------------------------------


async def _with_replicas(store: AnnotatorStore, **states: TaskState) -> str:
    """A labeling project holding the given task ids (`g1_r1` is `g1-r1`) at the given states; an id of the replica shape
    `{group}-r{k}` is a replica of its group, any other (a lookalike) an ordinary task."""
    project_id = await _project(store)
    ids = [name.replace("_", "-") for name in states]
    groups = {task_id: (m.group(1) if (m := re.fullmatch(r"(.+)-r\d+", task_id)) else None) for task_id in ids}
    await store.send_tasks(project_id, [_task(task_id, project_id, replica_of=groups[task_id]) for task_id in ids])
    # One annotator per replica: one person may hold or work only one replica of a group.
    for i, (task_id, state) in enumerate(zip(ids, states.values(), strict=True)):
        await _drive(store, task_id, state, annotator=f"annotator{i}")
    return project_id


@pytest.mark.asyncio
async def test_a_pick_is_re_pickable_while_adjudicable_and_the_last_one_stands(store: AnnotatorStore) -> None:
    project_id = await _with_replicas(store, g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)
    await store.adjudicate(project_id, group="g1", task_id="g1-r1", actor="meg")

    result = await store.adjudicate(project_id, group="g1", task_id="g1-r2", actor="ines")

    assert (result.adjudications["g1"].task_id, result.adjudications["g1"].by) == ("g1-r2", "ines")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "why"),
    [
        pytest.param("g2-r1", "not a replica of group g1", id="another-group"),
        pytest.param("g1-r9", "not in this project", id="absent"),
        pytest.param("g1-rogue", "not a replica of group g1", id="lookalike"),
        pytest.param("g1-r1-r2", "not a replica of group g1", id="nested-lookalike"),
        pytest.param("g1-r3", "is skipped, not accepted", id="not-accepted"),
    ],
)
async def test_a_pick_must_be_an_accepted_replica_of_the_group_in_this_project(store: AnnotatorStore, task_id: str, why: str) -> None:
    project_id = await _with_replicas(
        store, g1_r1=TaskState.ACCEPTED, g2_r1=TaskState.ACCEPTED, g1_r3=TaskState.SKIPPED, **{"g1_rogue": TaskState.ACCEPTED, "g1_r1_r2": TaskState.ACCEPTED}
    )

    with pytest.raises(IllegalTransition, match=why):
        await store.adjudicate(project_id, group="g1", task_id=task_id, actor="meg")


@pytest.mark.asyncio
async def test_a_pick_is_refused_once_provenance_is_frozen(store: AnnotatorStore) -> None:
    project_id = await _with_replicas(store, g1_r1=TaskState.ACCEPTED)
    await store.fire_project(project_id, event="freeze", actor="meg")
    await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)

    with pytest.raises(IllegalTransition, match="frozen"):
        await store.adjudicate(project_id, group="g1", task_id="g1-r1", actor="meg")


@pytest.mark.asyncio
async def test_clearing_a_pick_removes_it_and_clearing_an_absent_one_is_a_no_op(store: AnnotatorStore) -> None:
    project_id = await _with_replicas(store, g1_r1=TaskState.ACCEPTED)
    await store.adjudicate(project_id, group="g1", task_id="g1-r1", actor="meg")

    cleared = await store.adjudicate(project_id, group="g1", task_id=None, actor="meg")
    again = await store.adjudicate(project_id, group="g1", task_id=None, actor="meg")

    assert (cleared.adjudications, again.adjudications) == ({}, {})


@pytest.mark.asyncio
async def test_a_pick_is_voided_when_its_target_leaves_accepted_and_a_re_accept_does_not_restore_it(store: AnnotatorStore) -> None:
    project_id = await _with_replicas(store, g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)
    await store.adjudicate(project_id, group="g1", task_id="g1-r1", actor="meg")

    await store.fire_task("g1-r1", event="reopen", actor="meg")
    reopened = await store.get_project(project_id)
    await store.fire_task("g1-r1", event="claim", actor="annotator0")
    await store.fire_task("g1-r1", event="submit", actor="annotator0")
    await store.fire_task("g1-r1", event="accept", actor="carol")

    after = await store.get_project(project_id)
    assert reopened is not None and reopened.adjudications == {}
    assert after is not None and after.adjudications == {}


@pytest.mark.asyncio
async def test_dropping_a_picked_replica_takes_its_pick_with_it(store: AnnotatorStore) -> None:
    project_id = await _with_replicas(store, g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)
    await store.adjudicate(project_id, group="g1", task_id="g1-r1", actor="meg")

    await store.drop_task(project_id, "g1-r1")

    project = await store.get_project(project_id)
    assert project is not None and project.adjudications == {}
