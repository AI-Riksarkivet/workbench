"""The publish saga — project → lakehouse, crash-safe without a workflow engine.

Every step is idempotent by key, so running the saga again after any crash converges on one publish. **Creating the
table is the only step that is not naturally idempotent**, and it is made idempotent BY KEY: `AnnotationProject.
pending_publish_id` is minted once at the `publish` transition and reused by every retry, and the table id derives
from it, so a resumed publish finds the table it already created rather than making a second one.

| step | run twice |
| --- | --- |
| read the project and its tasks | pure read |
| build the plan | pure; a replay is byte-identical (`build_plan` sorts everything derived) |
| create the table | same id ⇒ already-exists, treated as success, NOT an error |
| record the publish | set once; the second call answers the first record |
| fire `publish_succeeded` | already `published` ⇒ `IllegalTransition`, swallowed as converged |

The same table holds for two runs at once, which a lease that lapsed under a slow step allows: both create the one
table id, the record is set once, and the later `publish_succeeded` converges. A run that finds its lease gone
before a step stops with nothing recorded (`LostLease`).

The tasks are read from the store in one statement while the project is `publishing`, a state in which no task
transition is taken (`annotator.state.store`), so the plan is built from the rows the `publish` transition judged.

The transport is injected (`Publisher`), so every path here runs against a real store with no catalog.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ProjectState, PublishRecord
from annotator.projects.publish import PublishPlan, PublishRefusal, build_plan, publish_inputs, table_properties
from service_kit.naming import CATALOG_DELIMITER


if TYPE_CHECKING:
    from annotator.state.store import AnnotatorStore


logger = logging.getLogger(__name__)

#: The most inputs one write's run may name: each is one authorization check on the catalog's commit path. Not in the
#: published contract; read off rask `services/catalog/src/catalog/core/run_context.py` (`MAX_INPUTS`) at `d057ecfb4`.
MAX_INPUTS = 64


class Publisher(Protocol):
    """The lakehouse side of the saga, injected so the orchestration is testable without a catalog.

    Implementations post through the catalog's REST API — **the annotator never writes Lance
    directly** (§7.1), because the catalog is what seeds FGA ownership on the new table and emits
    the `CREATE` RunEvent with the caller's verified identity.
    """

    @property
    def lakehouse(self) -> str:
        """The name of the lakehouse source it writes to (`client_kit.sources`): only that source's tables are inputs."""
        ...

    async def create_table(
        self,
        table_id: str,
        plan: PublishPlan,
        *,
        properties: dict[str, str],
        run_id: str,
        inputs: list[tuple[str, int]],
    ) -> int:
        """Create the table from the plan's rows; return its version. MUST treat an existing table
        with this id as success — the saga relies on that for retry safety. ``run_id`` and ``inputs`` (each captured
        table at its version) travel as the catalog Context's run and inputs."""
        ...

    async def requires_run(self, namespace: str) -> bool:
        """Whether the lakehouse requires a write under ``namespace`` to name its run and inputs: any namespace on its
        path declares the run rule (`docs/rask/WRITE-GOVERNANCE.md`)."""
        ...


class PublishOutcome(BaseModel):
    """What the saga did. `already_published` distinguishes a converged retry from fresh work —
    without it a caller cannot tell "I published this" from "this was already published"."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    publish_id: str
    table_id: str
    version: int
    rows: int
    shapes: int
    already_published: bool = False


# The catalog's identifier delimiter is `CATALOG_DELIMITER` (imported): estate ids are `acme$media$events`,
# `acme$published$features`. A `.` here is NOT a separator to the catalog — the id would parse as one root-level
# segment, landing the table at the catalog ROOT while FGA authorized creation in `namespace:<target>`,
# so the authorization object and the created object would diverge.
def table_id_for(project: AnnotationProject, publish_id: str, namespace: str) -> str:
    """The published table's id — DETERMINISTIC in the publish token, which is what makes `create`
    idempotent on retry.

    The token, not the slug alone: two publishes of the same project (a republish after `reopen`)
    are two datasets and must not collide. The slug is carried for human legibility only.
    """
    return f"{namespace}{CATALOG_DELIMITER}{project.slug}_{publish_id[:12]}"


def _run_rule_refusal(namespace: str, uncaptured: int, outside: int) -> str:
    """Why a namespace under the run rule refuses this publish, naming only the causes that hold and the remedy each has:
    an item source with no captured version is sent again; a table outside the lakehouse can never be named, so only
    another namespace takes it."""
    causes = []
    if uncaptured:
        causes.append(f"{uncaptured} of this project's item sources recorded no version of a catalog table (send those items again from their dataset)")
    if outside:
        verb = "is a table" if outside == 1 else "are tables"
        causes.append(f"{outside} of this project's item sources {verb} outside the lakehouse, which no lakehouse write can name")
    if not causes:
        causes.append("none of this project's items named a table")
    return (
        f"{namespace} requires every write to name the tables it was made from, and {'; '.join(causes)}: publish into a namespace that does not require a run"
    )


class LostLease(Exception):
    """This run no longer holds its project's publish lease: another run drives the publish now, so this one stops
    without recording anything."""


async def run_publish(
    *,
    store: AnnotatorStore,
    project_id: str,
    publisher: Publisher,
    namespace: str,
    subject: str,
    renew: Callable[[], Awaitable[None]] | None = None,
) -> PublishOutcome:
    """Run the saga to completion, or fire `publish_failed` and re-raise.

    Safe to call again after any crash, and safe beside a second run of the same publish: every step converges on the
    same `pending_publish_id`. ``renew`` extends the run's lease before each step and raises `LostLease` when it holds
    none any more, which ends the run with nothing recorded.
    """

    async def step(name: str) -> None:
        if renew is not None:
            await renew()
        await _note(store, project_id, name)

    project = await store.get_project(project_id)
    if project is None:
        raise PublishRefusal("annotation project does not exist")

    if project.published is not None:
        # A retry that got all the way through last time. A crash between recording the publish and
        # `publish_succeeded` leaves the record set while the project is still `publishing`, which has no principal
        # edge out: converge the state first, or the project is stranded there.
        if project.state is ProjectState.PUBLISHING:
            await _converge(store, project_id, "publish_succeeded", subject)
        record = project.published
        return PublishOutcome(
            project_id=project.project_id,
            publish_id=record.publish_id,
            table_id=record.table_id,
            version=record.version,
            rows=0,
            shapes=0,
            already_published=True,
        )

    if project.state is not ProjectState.PUBLISHING:
        raise PublishRefusal(f"project {project.project_id} is {project.state}, not publishing — the saga runs only after the publish transition")
    publish_id = project.pending_publish_id
    published_at = project.pending_publish_at

    try:
        # Inside the try, unlike the two refusals above: this one describes a project that IS publishing, and
        # `publish_failed` is its only exit and a resting state from which it can be published again.
        if not publish_id or published_at is None:
            # The `publish` transition mints both. Guessing a token here would defeat the idempotency argument.
            raise PublishRefusal(f"project {project.project_id} is publishing but carries no publish token or instant — refusing to mint one here")

        await step("reading tasks")
        material = await store.publish_material(project_id)

        await step("building plan")
        # Every task's shapes, up to 10,000 each: built in a thread, so the loop keeps serving while it runs.
        plan = await asyncio.to_thread(build_plan, project, material, publish_id=publish_id, published_at=published_at)
        table_id = table_id_for(project, publish_id, namespace)

        # §7.2: every captured (table, version) is an input, and none is fabricated. Refused before anything is written
        # when the catalog could not take them: more than one run may name, or, under the run rule, any item that
        # captured none.
        inputs = publish_inputs(plan, publisher.lakehouse)
        if len(inputs.tables) > MAX_INPUTS:
            raise PublishRefusal(
                f"the project's items came from {len(inputs.tables)} table versions, and one publish names at most {MAX_INPUTS} inputs: "
                "split the items across projects"
            )
        if (inputs.uncaptured or inputs.outside or not inputs.tables) and await publisher.requires_run(namespace):
            raise PublishRefusal(_run_rule_refusal(namespace, inputs.uncaptured, inputs.outside))

        await step(f"creating table {table_id}")
        version = await publisher.create_table(
            table_id,
            plan,
            properties=table_properties(project, plan),
            inputs=inputs.tables,
            # The publish token names the run: minted once per publish, so a retried create names the same one.
            run_id=publish_id,
        )
        await step("recording publish")
        record = await store.record_publish(
            project_id,
            PublishRecord(
                table_id=table_id,
                namespace=namespace,
                version=version,
                publish_id=publish_id,
                published_at=published_at,
                published_by=subject,
            ),
        )
        await _converge(store, project_id, "publish_succeeded", subject)
        return PublishOutcome(
            project_id=project.project_id,
            publish_id=publish_id,
            table_id=record.table_id,
            version=record.version,
            rows=len(plan.rows),
            shapes=plan.shape_count,
        )
    except LostLease:
        # Another run holds the publish: what is left is its to finish, and recording a failure would undo its work.
        logger.info("publish of project %s (token %s) is driven by another run now; this one stops", project.project_id, publish_id)
        raise
    except Exception as exc:
        # The log comes FIRST and the recording is itself guarded: a failure while recording the failure must not
        # replace the original exception, or the operator is shown the second-order error and the cause is never logged.
        logger.exception("publish saga failed for project %s (token %s)", project.project_id, publish_id)
        try:
            await _converge(store, project_id, "publish_failed", subject, error=str(exc))
        except Exception:
            logger.exception("could not record the publish failure for project %s — the original error stands", project.project_id)
        raise


async def _note(store: AnnotatorStore, project_id: str, step: str) -> None:
    """Narrate the saga's current step onto the project — A4's "not just a spinner".

    Best-effort by contract: progress is telemetry for a watching human, and a narration failure must never fail a
    publish. The store ignores a note once the project has left `publishing`."""
    try:
        await store.note_progress(project_id, step)
    except Exception:
        logger.debug("could not record publish progress %r", step, exc_info=True)


async def _converge(store: AnnotatorStore, project_id: str, event: str, subject: str, *, error: str | None = None) -> None:
    """Fire a terminal transition, tolerating the case where a previous attempt already fired it.

    `IllegalTransition` here means the project is ALREADY in the state we were driving it to, which
    is convergence, not failure. Anything else is a real problem and propagates.
    """
    try:
        await store.fire_project(project_id, event=event, actor=subject, error=error)
    except IllegalTransition:
        logger.info("project already in the target state for %s — converged", event)
