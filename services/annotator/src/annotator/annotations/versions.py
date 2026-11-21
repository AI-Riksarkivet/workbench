"""A unit's history — the saves of a task's work, read off the bounded history the store keeps of its draft.

Who saved, when, and how many shapes the work held at each save (`annotator.state.store.AnnotatorStore.draft_history`,
at most `MAX_DRAFT_HISTORY` per task), newest first; a past save's rows are read by the wire door's ``?version=``.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from annotator.annotations.access import ReadableUnit, UnitKey
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET
from annotator.state.store import MAX_DRAFT_HISTORY
from workbench.media.deps import DatasetParam


router = APIRouter(tags=["annotate"])


class AnnotationVersion(BaseModel):
    """One save of a task's work: its version (the draft's revision), when and by whom it was saved, and how many
    annotations the work held."""

    version: int
    timestamp: str
    author: str
    #: The name the author last wrote under, where one is kept (`annotator.people`).
    author_name: str | None = None
    count: int


@router.get("/annotations/{key:path}/versions", dependencies=[READS_DATASET])
async def annotation_versions(
    store: StoreDep,
    task: ReadableUnit,
    key: UnitKey,
    dataset: DatasetParam = None,
    limit: Annotated[int, Query(ge=1, le=MAX_DRAFT_HISTORY)] = 20,
) -> list[AnnotationVersion]:
    """The unit's edit history, most recent first: each save the history keeps of the task's work."""
    kept = await store.draft_history(task.task_id, limit=limit)
    names = await store.names_of(r.author for r in kept)
    return [
        AnnotationVersion(version=r.revision, timestamp=r.saved_at.isoformat(), author=r.author, author_name=names.get(r.author), count=r.shapes) for r in kept
    ]
