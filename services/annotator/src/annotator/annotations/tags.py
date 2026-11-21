"""Batch tag writes — whole-item labels as tag rows of a task's work.

A workflow's tags of a task's item become `tag` shapes in the task's draft (a human ``set``: ``source='human'``,
``status='accepted'``), all of a batch in ONE revision. Not a model deriver, so it does not funnel through the jobs
seam. An add is insert-if-absent by a deterministic id, so a re-tag is idempotent and never overwrites a reviewer's edit
of the row; a removal deletes by that id.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import EditableTask, require_task_units
from annotator.annotations.schema import SaveResult, TagBatch, TagWrite
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep, CurrentSubject
from annotator.corpus import resolve
from annotator.people import RECORDS_PERSON
from annotator.projects.models import Draft, Link, Shape, TaskState
from workbench.lancekit.keys import validate_doc_key
from workbench.media.deps import DatasetParam, StateDep


logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotate"], dependencies=RECORDS_PERSON)


def tag_id(task: str, doc_id: str, keys: Sequence[int], label: str) -> str:
    """Deterministic id for a TAG row — ``tag:{task}:{doc}:{keys}:{enc(label)}``.
    Idempotent (re-tagging the same unit maps to the same id, so no duplicates); the ``tag:`` prefix and the
    percent-encoded label keep it from ever equalling a drawn shape's random id. The task is in it because two tasks may
    hold one unit (a consensus item's replicas), each tagging it as its own work."""
    parts = [task, doc_id, *(str(k) for k in keys), quote(label, safe="")]
    return "tag:" + ":".join(parts)


def tagged(current: Draft | None, batch: TagBatch, *, task: str, doc_ids: dict[int, str], author: str, now: datetime) -> tuple[list[Shape], list[Link]]:
    """The draft's shapes and links once ``batch`` is applied: each add a tag row unless the draft holds its id, each
    removal's row gone, with any link touching it. ``doc_ids`` is each write's validated doc key, by its place in
    ``adds + removes``."""
    shapes = {s.shape_id: s for s in (current.shapes if current else [])}
    for place, write in enumerate(batch.adds):
        for label in write.labels:
            tid = tag_id(task, doc_ids[place], write.keys, label)
            if tid not in shapes:
                shapes[tid] = Shape(shape_id=tid, shape_type="tag", label=label, source="human", reviewer=author, created_at=now, updated_at=now)
    gone = {tag_id(task, doc_ids[len(batch.adds) + place], write.keys, label) for place, write in enumerate(batch.removes) for label in write.labels}
    links = [link for link in (current.links if current else []) if link.from_shape not in gone and link.to_shape not in gone]
    return [s for sid, s in shapes.items() if sid not in gone], links


def _unit_key(write: TagWrite) -> str:
    return "/".join([write.doc_id, *(str(k) for k in write.keys)])


@router.post("/annotations/tags", dependencies=[READS_DATASET])
async def apply_tags(
    task: EditableTask,
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    # The VERIFIED subject, never a header: `anon` with sign-in off, the token's principal with it on.
    author: CurrentSubject,
    body: TagBatch,
    dataset: DatasetParam = None,
) -> SaveResult:
    """Apply a TagBatch to the task's work: adds insert-if-absent, removes delete by the deterministic id. Idempotent; a
    batch reaches only the named task's draft, and every unit it tags must be that task's (`require_task_units`)."""
    corpus = await run_in_threadpool(resolve, state, dataset, caller)
    writes = [*body.adds, *body.removes]
    require_task_units(task, [_unit_key(w) for w in writes], dataset)
    doc_ids = {place: validate_doc_key(corpus.declared, w.doc_id) for place, w in enumerate(writes)}

    def change(current: Draft | None, now: datetime) -> tuple[list[Shape], list[Link]]:
        return tagged(current, body, task=task.task_id, doc_ids=doc_ids, author=author, now=now)

    draft = await store.change_draft(
        task.task_id,
        author=author,
        change=change,
        base_revision=body.base_version,
        require_base=False,
        reviewing=task.state is TaskState.IN_REVIEW,
    )
    touched = sum(len(w.labels) for w in writes)
    logger.info("tagged task %s → v%d (%d add(s), %d removal(s))", task.task_id, draft.revision, len(body.adds), len(body.removes))
    return SaveResult(saved=touched, version=draft.revision)
