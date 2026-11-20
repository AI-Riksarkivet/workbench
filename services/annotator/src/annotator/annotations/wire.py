"""Arrow-IPC serving — the canvas's READ of a unit's work.

The canvas's engine consumes Arrow IPC directly (zero-copy geometry views), so the task's draft, the one store of its
work in progress, is served as an Arrow IPC stream of rows (`annotator.annotations.work.wire_table`), the same wire
format the atlas's ``/points`` uses. ``X-Annotations-Version`` is the draft's revision (0 before the first save), which
a save echoes as its ``base_version``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, Response
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import ReadableUnit, UnitKey
from annotator.annotations.unit import parse_unit
from annotator.annotations.work import wire_table
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep
from annotator.corpus import resolve
from client_kit.media.deps import DatasetParam, StateDep
from service_kit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from service_kit.exceptions import NotFoundError


if TYPE_CHECKING:
    from annotator.annotations.unit import Unit
    from annotator.projects.models import Draft


router = APIRouter(tags=["annotate"])


@router.get("/annotations/{key:path}", dependencies=[READS_DATASET])
async def annotations(
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    task: ReadableUnit,
    key: UnitKey,
    dataset: DatasetParam = None,
    version: Annotated[int | None, Query(ge=1, description="Read a past save of the work, as the history keeps it.")] = None,
) -> Response:
    """Arrow IPC stream of a task's work on its unit (`annotator.annotations.access`): its draft, never another task's,
    even of the same unit.

    The key's segments pair with the dataset's identity fields (`annotator.annotations.unit`), which head every row.
    ``version`` reads a past save from the task's history (a 404 for one it does not keep); omitted, the draft now."""
    corpus = await run_in_threadpool(resolve, state, dataset, caller)
    unit = parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    if version is not None:
        past = await store.draft_at(task.task_id, version)
        if past is None:
            raise NotFoundError(f"annotations version {version} not found: this item's history does not keep it", extensions={"task": task.task_id})
        return await run_in_threadpool(_response, unit, task.task_id, past, version)
    draft = await store.get_draft(task.task_id)
    return await run_in_threadpool(_response, unit, task.task_id, draft, draft.revision if draft else 0)


def _response(unit: Unit, task_id: str, draft: Draft | None, version: int) -> Response:
    """The wire response: rows + the version the client echoes on Save. Blocking (a draft of up to 10,000 shapes built and
    encoded as Arrow), so the route runs it in the threadpool."""
    return Response(
        content=encode_arrow_stream(wire_table(unit, task_id, draft)),
        media_type=ARROW_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Annotations-Version": str(version)},
    )
