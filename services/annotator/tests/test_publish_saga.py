"""The publish saga — every crash point, replayed, on the real state store.

`docs/OPERATORS.md` §4 permits skipping a workflow engine ONLY for paths that are token-keyed idempotent. That is a
claim about this code, so it is tested as one: the saga is run, killed after a step, and run again, and the assertion
is always the same — one table, one publish record, one logical publish.

The project and its tasks are rows in a PostgreSQL database of the test's own; the catalog is the one collaborator
stood in for (`_Publisher`), with the contract the real one keeps: a create is idempotent on the table id.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from annotator.core.config import AnnotatorSettings
from annotator.projects.lakehouse import PublishRunner
from annotator.projects.models import AnnotationProject, ProjectState, PublishRecord, Shape, Task, TaskState
from annotator.projects.publish import PublishRefusal
from annotator.projects.saga import run_publish, table_id_for
from annotator.state.store import AnnotatorStore


TARGET = "acme$published"


@pytest_asyncio.fixture
async def store(state_settings: AnnotatorSettings) -> AsyncGenerator[AnnotatorStore]:
    opened = await AnnotatorStore.open(state_settings)
    yield opened
    await opened.close()


#: A lakehouse table and a table of an annotatable source outside it, as a send stamps them (`<source>~<id>`).
PAGES, SURVEY = "lake~acme$media$pages", "archive~field$survey$chunks"


class _Publisher:
    """A catalog that behaves like the real one must: create is idempotent on table id."""

    #: The lakehouse source it writes to: only that source's tables are inputs.
    lakehouse = "lake"

    def __init__(self, fail_on: str | None = None, *, run_rule: bool = False) -> None:
        self.tables: dict[str, int] = {}
        self.creates = 0
        self.inputs: list[tuple[str, int]] | None = None
        self.fail_on = fail_on
        #: Whether the target namespace declares the run rule, as the catalog would answer.
        self.run_rule = run_rule

    async def requires_run(self, namespace: str) -> bool:
        return self.run_rule

    async def create_table(
        self,
        table_id: str,
        plan: Any,
        *,
        properties: dict[str, str],
        run_id: str,
        inputs: list[tuple[str, int]],
    ) -> int:
        self.inputs = inputs
        if self.fail_on == "create":
            raise RuntimeError("catalog unreachable")
        self.creates += 1
        # The contract the saga relies on: an existing id is success, not an error.
        self.tables.setdefault(table_id, 7)
        self.rows = list(plan.rows)
        self.run_id = run_id
        if self.fail_on == "after-create":
            raise RuntimeError("the create committed and its answer was lost")
        return self.tables[table_id]


async def _publishing(store: AnnotatorStore, *tasks: tuple[str, TaskState, int, str | None, int | None], publish: bool = True) -> str:
    """A project whose tasks are `(id, terminal state, shapes drawn, captured table, captured version)`, frozen and
    taken through the `publish` transition, as a person drives it."""
    project = AnnotationProject(tenant="acme", slug="vasa", state=ProjectState.LABELING)
    await store.create_project(project)
    await store.send_tasks(
        project.project_id,
        [
            Task.model_validate(
                {
                    "task_id": task_id,
                    "project_id": project.project_id,
                    "source": {"kind": "chunks", "keys": [task_id], "where": table or "lake~acme$notes", "table": table, "dataset_version": version},
                    "media": {"kind": "image"},
                }
            )
            for task_id, _state, _shapes, table, version in tasks
        ],
    )
    for task_id, state, shapes, _table, _version in tasks:
        await store.fire_task(task_id, event="claim", actor="gina")
        if shapes:
            drawn = [Shape(shape_type="bbox", x=1.0, y=1.0, width=1.0, height=1.0) for _ in range(shapes)]
            await store.save_draft(task_id, author="gina", shapes=drawn, links=[], base_revision=None)
        if state is TaskState.SKIPPED:
            await store.fire_task(task_id, event="skip", actor="gina")
        else:
            await store.fire_task(task_id, event="submit", actor="gina")
            await store.fire_task(task_id, event="accept", actor="carol")
    await store.fire_project(project.project_id, event="freeze", actor="meg")
    if publish:
        await store.fire_project(project.project_id, event="publish", actor="henry", target_namespace=TARGET)
    return project.project_id


async def _run(store: AnnotatorStore, project_id: str, publisher: _Publisher) -> Any:
    return await run_publish(store=store, project_id=project_id, publisher=publisher, namespace=TARGET, subject="henry")


async def _state(store: AnnotatorStore, project_id: str) -> AnnotationProject:
    project = await store.get_project(project_id)
    assert project is not None
    return project


ONE_ACCEPTED = ("t0", TaskState.ACCEPTED, 1, None, None)


# --------------------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_publish_creates_one_table_and_records_it(store: AnnotatorStore) -> None:
    project_id = await _publishing(store, ("t0", TaskState.ACCEPTED, 2, None, None), ("t1", TaskState.SKIPPED, 0, None, None))
    publisher = _Publisher()

    out = await _run(store, project_id, publisher)

    project = await _state(store, project_id)
    assert (out.shapes, out.rows) == (2, 3)  # 2 shapes + 1 skip sentinel
    assert publisher.creates == 1
    assert project.state is ProjectState.PUBLISHED
    assert project.published is not None and project.published.publish_id == project.pending_publish_id == publisher.run_id


@pytest.mark.asyncio
async def test_the_saga_narrates_its_steps_in_order_so_a_publish_is_not_just_a_spinner(store: AnnotatorStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A4: a watching human sees WHERE the publish is. The steps reach the project in execution order, named after the
    real work; the project shows the step running now."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    steps: list[str] = []
    recorded = store.note_progress

    async def note(project: str, step: str) -> None:
        steps.append(step)
        await recorded(project, step)

    monkeypatch.setattr(store, "note_progress", note)

    await _run(store, project_id, _Publisher())

    assert steps[:2] == ["reading tasks", "building plan"]
    assert steps[2].startswith("creating table acme$published$vasa_")
    assert steps[3:] == ["recording publish"]


@pytest.mark.asyncio
async def test_a_narration_failure_does_not_fail_the_publish(store: AnnotatorStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress is telemetry, not a step: a note the store refuses must not turn a working publish into a failed one."""
    project_id = await _publishing(store, ONE_ACCEPTED)

    async def refuse(_project: str, _step: str) -> None:
        raise RuntimeError("note refused")

    monkeypatch.setattr(store, "note_progress", refuse)

    out = await _run(store, project_id, _Publisher())

    assert out.rows == 1
    assert (await _state(store, project_id)).state is ProjectState.PUBLISHED


def test_the_table_id_is_derived_from_the_token_not_the_slug_alone() -> None:
    """Two publishes of one project (a republish after `reopen`) are two datasets. A slug-only id
    would collide and silently overwrite the first."""
    p = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    a = table_id_for(p, "aaaaaaaaaaaaaaaa", TARGET)
    b = table_id_for(p, "bbbbbbbbbbbbbbbb", TARGET)

    assert a != b
    assert a.startswith("acme$published$vasa_")
    assert table_id_for(p, "aaaaaaaaaaaaaaaa", TARGET) == a, "the id must be deterministic in the token"


# --------------------------------------------------------------------------------------------------
# Crash and retry — the claim OPERATORS.md §4 requires
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_after_create_retries_onto_the_same_table(store: AnnotatorStore) -> None:
    """The saga's only non-idempotent step, made idempotent by key: the retry the door fires reuses the token, so a
    second attempt does not create a second table."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    publisher = _Publisher(fail_on="after-create")

    with pytest.raises(RuntimeError):
        await _run(store, project_id, publisher)
    failed = await _state(store, project_id)
    await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)
    publisher.fail_on = None
    out = await _run(store, project_id, publisher)

    assert failed.state is ProjectState.PUBLISH_FAILED
    assert list(publisher.tables) == [out.table_id], "the retry created a SECOND table — the token was not reused"
    assert (await _state(store, project_id)).state is ProjectState.PUBLISHED


@pytest.mark.asyncio
async def test_a_failure_records_the_reason_and_leaves_a_retryable_state(store: AnnotatorStore) -> None:
    project_id = await _publishing(store, ONE_ACCEPTED)

    with pytest.raises(RuntimeError, match="catalog unreachable"):
        await _run(store, project_id, _Publisher(fail_on="create"))

    project = await _state(store, project_id)
    assert (project.state, project.publish_error) == (ProjectState.PUBLISH_FAILED, "catalog unreachable")
    assert project.publish_progress is not None and project.publish_progress.startswith("creating table"), "the failed step must stay named"


@pytest.mark.asyncio
async def test_running_the_saga_again_after_success_converges_instead_of_republishing(store: AnnotatorStore) -> None:
    """A second start of one publish must not produce a second dataset for it."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    publisher = _Publisher()
    first = await _run(store, project_id, publisher)

    second = await _run(store, project_id, publisher)

    assert second.already_published is True
    assert second.table_id == first.table_id
    assert publisher.creates == 1, "a replay republished the project"


@pytest.mark.asyncio
async def test_a_crash_between_record_and_succeeded_does_not_strand_the_project(store: AnnotatorStore) -> None:
    """`record_publish` lands, then the process dies before `publish_succeeded`: the project is `publishing` WITH a
    publish record, and `publishing` has no principal edge out. The next run converges the state before reporting."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    pending = await _state(store, project_id)
    assert pending.pending_publish_id is not None and pending.pending_publish_at is not None
    await store.record_publish(
        project_id,
        PublishRecord(
            table_id=table_id_for(pending, pending.pending_publish_id, TARGET),
            namespace=TARGET,
            version=7,
            publish_id=pending.pending_publish_id,
            published_at=pending.pending_publish_at,
            published_by="henry",
        ),
    )
    publisher = _Publisher()

    out = await _run(store, project_id, publisher)

    assert out.already_published is True
    assert (await _state(store, project_id)).state is ProjectState.PUBLISHED, "the run left the project stranded in publishing"
    assert publisher.creates == 0, "the run republished"


@pytest.mark.asyncio
async def test_a_failure_while_recording_the_failure_keeps_the_original_error(store: AnnotatorStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the operator is shown the store's error instead of the one that broke the publish."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    fire = store.fire_project

    async def wedged(project: str, *, event: str, **kwargs: Any) -> AnnotationProject:
        if event == "publish_failed":
            raise RuntimeError("the store is unreachable too")
        return await fire(project, event=event, **kwargs)

    monkeypatch.setattr(store, "fire_project", wedged)

    with pytest.raises(RuntimeError, match="catalog unreachable"):
        await _run(store, project_id, _Publisher(fail_on="create"))


# --------------------------------------------------------------------------------------------------
# Preconditions on the saga itself
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_saga_refuses_to_run_outside_publishing(store: AnnotatorStore) -> None:
    project_id = await _publishing(store, ONE_ACCEPTED, publish=False)

    with pytest.raises(PublishRefusal, match="not publishing"):
        await _run(store, project_id, _Publisher())

    assert (await _state(store, project_id)).state is ProjectState.FROZEN


@pytest.mark.asyncio
async def test_a_missing_token_stops_rather_than_minting_one(store: AnnotatorStore) -> None:
    """Inventing a token here would defeat the idempotency argument: a retry would mint a different one and create a
    second table. A publishing project that carries none is refused, and the refusal is recorded."""
    project = AnnotationProject(tenant="acme", slug="vasa", state=ProjectState.PUBLISHING)
    await store.create_project(project)
    publisher = _Publisher()

    with pytest.raises(PublishRefusal, match="no publish token"):
        await _run(store, project.project_id, publisher)

    assert publisher.creates == 0
    assert (await _state(store, project.project_id)).state is ProjectState.PUBLISH_FAILED


@pytest.mark.asyncio
async def test_every_captured_table_version_reaches_the_publisher_as_an_input(store: AnnotatorStore) -> None:
    """§7.2: the create names each table version the items came from; the saga computes them from the plan."""
    project_id = await _publishing(
        store, ("t0", TaskState.ACCEPTED, 1, "lake~acme$media$pages", 24), ("t1", TaskState.ACCEPTED, 1, "lake~acme$media$letters", 3)
    )
    publisher = _Publisher(run_rule=True)

    await _run(store, project_id, publisher)

    assert publisher.inputs == [("lake~acme$media$letters", 3), ("lake~acme$media$pages", 24)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_rule", "sources", "refusal"),
    [
        pytest.param(True, [(PAGES, 24), (None, None)], "acme$published requires every write to name the tables", id="uncaptured-under-the-run-rule"),
        pytest.param(False, [(PAGES, 24), (None, None)], None, id="uncaptured-with-no-run-rule"),
        pytest.param(
            True,
            [(PAGES, 24), (SURVEY, 3)],
            "acme$published requires every write to name the tables it was made from, and 1 of this project's item sources is a table"
            " outside the lakehouse, which no lakehouse write can name: publish into a namespace that does not require a run",
            id="outside-the-lakehouse-under-the-run-rule",
        ),
        pytest.param(False, [(PAGES, 24), (SURVEY, 3)], None, id="outside-the-lakehouse-with-no-run-rule"),
        pytest.param(False, [(f"lake~acme$media$t{i}", 1) for i in range(65)], "one publish names at most 64 inputs", id="more-inputs-than-one-run-names"),
    ],
)
async def test_a_publish_the_catalog_could_not_take_fails_before_it_writes(
    store: AnnotatorStore, run_rule: bool, sources: list[tuple[str | None, int | None]], refusal: str | None
) -> None:
    """Every item's input is named or the run rule cannot be met, and the publish says so in plain words before any
    table is created; with no run rule an uncaptured item, or one read from a table outside the lakehouse, publishes and
    names no input."""
    project_id = await _publishing(store, *((f"t{i:02d}", TaskState.ACCEPTED, 1, table, version) for i, (table, version) in enumerate(sources)))
    publisher = _Publisher(run_rule=run_rule)

    if refusal is None:
        await _run(store, project_id, publisher)
    else:
        with pytest.raises(PublishRefusal, match=re.escape(refusal)):
            await _run(store, project_id, publisher)

    project = await _state(store, project_id)
    assert publisher.creates == (1 if refusal is None else 0)
    assert publisher.inputs == ([(PAGES, 24)] if refusal is None else None)
    assert project.state is (ProjectState.PUBLISHED if refusal is None else ProjectState.PUBLISH_FAILED)
    if refusal is not None:
        assert project.publish_error is not None and refusal in project.publish_error


# --------------------------------------------------------------------------------------------------
# The publish lease: who drives a publish, in any replica
# --------------------------------------------------------------------------------------------------


class _HeldPublisher(_Publisher):
    """A catalog whose create does not answer until the test lets it: a run is then mid-publish, holding its lease and
    no connection."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.answer = asyncio.Event()
        self.in_create = 0
        self.most_in_create = 0

    async def create_table(self, table_id: str, plan: Any, *, properties: dict[str, str], run_id: str, inputs: list[tuple[str, int]]) -> int:
        self.in_create += 1
        self.most_in_create = max(self.most_in_create, self.in_create)
        self.entered.set()
        try:
            await self.answer.wait()
        finally:
            self.in_create -= 1
        return await super().create_table(table_id, plan, properties=properties, run_id=run_id, inputs=inputs)


def _runner(store: AnnotatorStore, publisher: _Publisher, *, lease_seconds: int = 300, concurrency: int = 2) -> PublishRunner:
    return PublishRunner(store, None, lease_seconds=lease_seconds, redrive_seconds=30, concurrency=concurrency, publisher_for=lambda _subject: publisher)


async def _until(condition: Any, what: str, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not await condition():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_publish_whose_run_stopped_is_finished_by_another_runner_and_the_late_run_records_nothing(store: AnnotatorStore) -> None:
    """A run that stops renewing (its process hung or died) loses its lease at the expiry, and another runner's sweep
    claims the publish and finishes it, with no restart. When the stalled run wakes, its create lands on the same table
    id and its next renewal finds the lease gone, so it stops: one table, one record, and no `publish_failed` over the
    finished publish."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    stalled, finisher = _HeldPublisher(), _Publisher()
    first, second = _runner(store, stalled, lease_seconds=1), _runner(store, finisher)
    run = first.spawn(project_id)
    await stalled.entered.wait()

    held_while_live = await second.sweep()
    await _until(lambda: second.sweep(), "the lapsed lease was never claimed by the second runner")
    await _until(lambda: _published(store, project_id), "the second runner did not finish the publish")
    stalled.answer.set()
    assert run is not None
    await run

    project = await _state(store, project_id)
    assert held_while_live == []
    assert (project.state, project.publish_error) == (ProjectState.PUBLISHED, None)
    assert project.published is not None and list(finisher.tables) == [project.published.table_id]
    # The stalled create landed after all: on the same table id, which the catalog's `exist_ok` makes the one table.
    assert list(stalled.tables) == list(finisher.tables)


async def _published(store: AnnotatorStore, project_id: str) -> bool:
    return (await _state(store, project_id)).state is ProjectState.PUBLISHED


@pytest.mark.asyncio
async def test_two_runners_sweeping_one_publish_drive_it_once(store: AnnotatorStore) -> None:
    project_id = await _publishing(store, ONE_ACCEPTED)
    publisher = _HeldPublisher()
    first, second = _runner(store, publisher), _runner(store, publisher)

    together = await asyncio.gather(first.sweep(), second.sweep(), first.sweep(), second.sweep())
    await publisher.entered.wait()
    while_live = await asyncio.gather(first.sweep(), second.sweep())
    in_create = publisher.in_create
    publisher.answer.set()
    await _until(lambda: _published(store, project_id), "the publish did not finish")
    await first.close()
    await second.close()

    assert sorted(id_ for claimed in together for id_ in claimed) == [project_id], "two sweeps at one moment both claimed"
    assert while_live == [[], []], "a sweep claimed a publish whose run's lease is live"
    assert (in_create, publisher.most_in_create, publisher.creates) == (1, 1, 1)


@pytest.mark.asyncio
async def test_more_publishes_than_a_runner_takes_leave_the_pool_to_requests(state_settings: AnnotatorSettings) -> None:
    """A runner claims no more publishes than its bound, and a run holds no connection while the catalog takes its time,
    so a small pool keeps serving requests while every run is mid-publish."""
    small = await AnnotatorStore.open(state_settings.model_copy(update={"state_pool_size": 3}))
    try:
        projects = [await _publishing(small, (f"t{i}", TaskState.ACCEPTED, 1, None, None)) for i in range(5)]
        publisher = _HeldPublisher()
        runner = _runner(small, publisher, concurrency=2)
        claimed = await runner.sweep()
        await _until(lambda: _in_create(publisher, 2), "the claimed runs never reached the catalog")

        started = time.monotonic()
        reads = await asyncio.wait_for(asyncio.gather(*(small.get_project(p) for p in projects * 4)), timeout=10)
        took = time.monotonic() - started
        publisher.answer.set()
        await runner.close()
    finally:
        await small.close()

    assert len(claimed) == 2
    assert all(read is not None for read in reads)
    assert took < 5, f"20 reads beside two publishes in flight took {took:.1f} s on a pool of 3"


async def _in_create(publisher: _HeldPublisher, runs: int) -> bool:
    return publisher.in_create == runs


@pytest.mark.asyncio
async def test_a_publish_retried_before_the_failed_run_let_go_of_its_lease_gets_a_run(store: AnnotatorStore) -> None:
    """A failed run's `publish_failed` ends its lease with the state, so the retry the person fires next is claimable at
    once, even though the failed run has not yet released it; that release, arriving late, leaves the retry's lease."""
    project_id = await _publishing(store, ONE_ACCEPTED)
    assert await store.claim_publish("old-run", 300, project_id=project_id) == [project_id]
    await store.fire_project(project_id, event="publish_failed", actor="henry", error="catalog unreachable")
    await store.fire_project(project_id, event="publish", actor="henry", target_namespace=TARGET)
    publisher = _HeldPublisher()
    runner = _runner(store, publisher)

    run = runner.spawn(project_id)
    await publisher.entered.wait()
    await store.release_publish(project_id, "old-run")
    publisher.answer.set()
    assert run is not None
    await run

    project = await _state(store, project_id)
    assert (project.state, project.publish_error) == (ProjectState.PUBLISHED, None)
