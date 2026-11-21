"""Undo one save of a task's work, on the server, from the undo its history kept.

A client that changed many items at once (the bulk grid) undoes each item's change by naming the revision its save
wrote: the store puts back what that save changed or removed, whole (each shape's provenance, status, scores, text,
attributes and times) with the links as they were (`AnnotatorStore.revert_save`), rather than the client rebuilding the
shapes from what it read of them. Only while the work is still at that revision: one changed since by anyone is a 409, so
an undo never overwrites later work. It is a save in every other respect, under the same rules (`EditableUnit` here, and
again under the task's row lock), and is kept in the history as its own revision, so reverting the revert redoes it.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import EditableUnit, UnitKey
from annotator.annotations.schema import SaveResult
from annotator.annotations.unit import parse_unit
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep, CurrentSubject
from annotator.corpus import resolve
from annotator.people import RECORDS_PERSON
from annotator.projects.models import TaskState
from workbench.media.deps import DatasetParam, StateDep


logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotate"], dependencies=RECORDS_PERSON)


class RevertSave(BaseModel):
    """The save to undo, by the revision it wrote: the revision the work must still be at. Strict and closed, as the
    ontology's documents are: a revision written as text, or a key the door does not read, is a 422 naming it, never a
    guess at what the caller meant."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=1, le=2**31 - 1)


@router.post("/annotations/{key:path}/revert", dependencies=[READS_DATASET])
async def revert_save(
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    # The VERIFIED subject, never a header: `anon` with sign-in off, the token's principal with it on.
    author: CurrentSubject,
    task: EditableUnit,
    key: UnitKey,
    body: RevertSave,
    dataset: DatasetParam = None,
) -> SaveResult:
    """Undo the save that wrote ``body.revision`` of the task's work, as ONE new revision (see module docstring)."""
    corpus = await run_in_threadpool(resolve, state, dataset, caller)
    parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    # A task that moved between the door's read and the transaction is an IllegalTransition, a 409.
    draft, touched = await store.revert_save(task.task_id, author=author, revision=body.revision, reviewing=task.state is TaskState.IN_REVIEW)
    logger.info("reverted task %s's save v%d → v%d", task.task_id, body.revision, draft.revision)
    return SaveResult(saved=touched, version=draft.revision)
