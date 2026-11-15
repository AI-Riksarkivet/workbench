"""Whose annotations a door reads or writes: a unit of a task's item, in that task's project.

An annotations door names a project (`?project=`, whose table holds the rows), a task (`?task=`), a dataset and a unit
key. Each is checked against the task's own record, so a caller cannot write a project's table for a unit no task of
that project holds, nor for a task its role does not reach:

- The project is asked first: someone who may not view it gets D3a's one refusal (`unseen_project`, 404).
- The task must be one they may view, and the project's own: anything else is the task's one refusal (`unseen_task`,
  404), the same words for an absent task, a hidden one and another project's.
- The unit must be one of the task's item (its dataset and one of its keys), else a 404 naming the task and the unit.
- A write must also be an edit the task takes now (`tasks.require_editable`): 409 when its state or its project's takes
  none, 403 when the caller lacks the relation or is not who may edit it.

A tag batch names its task the same way, and every unit it tags must be one of that task's (`require_task_units`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Path, Query

from annotator.api.security import Checker, CheckerDep, CurrentSubject, ProjectParam, require_view, unseen_project, unseen_task
from annotator.api.v1.endpoints.tasks import require_editable, viewed_task
from annotator.audit_actions import AnnotatorAuditAction
from client_kit.media.deps import DatasetParam
from service_kit.exceptions import NotFoundError


#: The task whose item a unit is.
TaskParam = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="The task whose item this unit is")]
#: A unit's key path: `<doc key>/<field>/…`, one segment per identity field (`annotator.annotations.unit`).
UnitKey = Annotated[str, Path(min_length=1, max_length=512, description="The unit's key path, one segment per identity field")]


async def _project_task(task: str, project: str, subject: str, checker: Checker) -> dict[str, Any]:
    await require_view(checker, subject, f"project:{project}", unseen_project(project))
    current = await viewed_task(task, checker, subject)
    if current["project_id"] != project:
        raise NotFoundError(unseen_task(task))
    return current


def require_task_units(current: dict[str, Any], keys: list[str], dataset: str | None) -> None:
    """Each unit key is one of the task's item, in the task's dataset, or the 404 naming the first that is not."""
    source = current["source"]
    held = source.get("keys") or []
    for key in keys:
        if source.get("where") != dataset or key not in held:
            raise NotFoundError(f"unit {key} of dataset {dataset} is not task {current['task_id']}'s")


async def _task_unit(task: str, project: str, key: str, dataset: str | None, subject: str, checker: Checker) -> dict[str, Any]:
    current = await _project_task(task, project, subject, checker)
    require_task_units(current, [key], dataset)
    return current


async def readable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, dataset: DatasetParam = None
) -> None:
    """A read of a unit's annotations: the reader may view the project and the task, and the unit is the task's."""
    await _task_unit(task, project, key, dataset, subject, checker)


async def editable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, dataset: DatasetParam = None
) -> None:
    """A write of a unit's annotations: a readable unit, and an edit its task takes from this caller now."""
    current = await _task_unit(task, project, key, dataset, subject, checker)
    await require_editable(current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)


async def editable_task(task: TaskParam, project: ProjectParam, subject: CurrentSubject, checker: CheckerDep) -> dict[str, Any]:
    """A write across a task's units (a tag batch): the task is the project's, and takes an edit from this caller now.
    Answers the task's record, against which the route checks every unit it writes (`require_task_units`)."""
    current = await _project_task(task, project, subject, checker)
    await require_editable(current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)
    return current


EditableTask = Annotated[dict[str, Any], Depends(editable_task)]
MAY_READ_UNIT = Depends(readable_unit)
MAY_EDIT_UNIT = Depends(editable_unit)
