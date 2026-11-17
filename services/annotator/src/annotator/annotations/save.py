"""The per-unit Save — the canvas's change to a task's work, applied to its draft.

Edits accumulate client-side (an in-memory overlay with undo and redo); one Save sends only what changed (field edits,
new shapes, moved geometry, resized times, re-anchored spans, deletes and, when they changed, the item's links), and
the store applies it to the task's draft in one transaction under the task's row lock (`AnnotatorStore.change_draft`):
the revision the client loaded is checked there (409 when the work moved, per task), and so are the task's state, who
may edit it now and whether its project is frozen. Nothing is written to the lakehouse: work in progress lives in the
annotator's own database until a publish.
"""

import logging
from datetime import datetime

from fastapi import APIRouter

from annotator.annotations.access import EditableUnit, UnitKey
from annotator.annotations.schema import SaveAnnotations, SaveResult
from annotator.annotations.unit import parse_unit
from annotator.annotations.work import apply_save
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep, CurrentSubject
from annotator.corpus import resolve
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import Draft, Link, Shape, TaskState
from client_kit.media.deps import DatasetParam, StateDep
from service_kit.exceptions import ConflictError


logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotate"])


@router.post("/annotations/{key:path}", dependencies=[READS_DATASET])
async def save_annotations(
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    # The VERIFIED subject, never a header: `anon` with sign-in off, the token's principal with it on.
    author: CurrentSubject,
    task: EditableUnit,
    key: UnitKey,
    body: SaveAnnotations,
    dataset: DatasetParam = None,
) -> SaveResult:
    """Apply a save to the task's work as ONE new revision of its draft (see module docstring)."""
    corpus = resolve(state, dataset, caller)
    # The key is read against the dataset's identity, so a unit the dataset cannot have is refused in its own words.
    parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)

    def change(current: Draft | None, now: datetime) -> tuple[list[Shape], list[Link]]:
        return apply_save(current, body, author=author, now=now)

    try:
        draft = await store.change_draft(
            task.task_id,
            author=author,
            change=change,
            base_revision=body.base_version,
            # A verified input like the subject: the caller holds `can_review` on this task in review (`EditableUnit`).
            reviewing=task.state is TaskState.IN_REVIEW,
        )
    except IllegalTransition as exc:  # the task moved between the door's read and the transaction
        raise ConflictError(str(exc)) from exc
    touched = len(body.edits) + len(body.inserts) + len(body.geometry) + len(body.temporal) + len(body.spans) + len(body.deletes)
    logger.info("saved task %s's work on %s → v%d (%d row(s) named)", task.task_id, key, draft.revision, touched)
    return SaveResult(saved=touched, version=draft.revision)
