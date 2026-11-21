"""Keep or remove every proposal of an item at once.

An item cannot be submitted while it holds a proposal nobody kept or removed (ADR 0019), so settling them all is one
action: the canvas offers it on the open item, and the Label grid on each item it lists, which holds none of the item's
shapes and so asks the annotator to do it. It is a save in every other respect: under the rules of one (`EditableUnit`
here, and again under the task's row lock), on the revision the caller read (409 when the work moved), kept in the
history as one revision, so the save's own undo (`revert`) puts every proposal back as it was.
"""

import logging
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import EditableUnit, UnitKey
from annotator.annotations.unit import parse_unit
from annotator.annotations.work import SettleAction, settle_proposals
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep, CurrentSubject
from annotator.corpus import resolve
from annotator.people import RECORDS_PERSON
from annotator.projects.models import Draft, Link, Shape, TaskState
from workbench.media.deps import DatasetParam, StateDep


logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotate"], dependencies=RECORDS_PERSON)


class SettleProposals(BaseModel):
    """What to do with every proposal the item holds, and the revision of its work the caller read."""

    model_config = ConfigDict(extra="forbid")

    action: SettleAction
    base_version: int = Field(ge=0, le=2**31 - 1)


class Settled(BaseModel):
    """How many proposals were kept or removed, and the revision of the work now (the one read, when there were none)."""

    settled: int
    version: int


@router.post("/annotations/{key:path}/proposals", dependencies=[READS_DATASET])
async def settle(
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    # The verified subject, never a header: a kept proposal records them as the person who kept it.
    author: CurrentSubject,
    task: EditableUnit,
    key: UnitKey,
    body: SettleProposals,
    dataset: DatasetParam = None,
) -> Settled:
    """Keep or remove every proposal of the task's work, as one new revision of its draft."""
    corpus = await run_in_threadpool(resolve, state, dataset, caller)
    parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    counted: list[int] = []

    def change(current: Draft | None, now: datetime) -> tuple[list[Shape], list[Link]]:
        shapes, links, count = settle_proposals(current, body.action, author=author, now=now)
        counted.append(count)
        return shapes, links

    draft = await store.change_draft(task.task_id, author=author, change=change, base_revision=body.base_version, reviewing=task.state is TaskState.IN_REVIEW)
    logger.info("task %s: %d proposal(s) settled (%s) → v%d", task.task_id, counted[-1], body.action, draft.revision)
    return Settled(settled=counted[-1], version=draft.revision)
