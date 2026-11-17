"""Whose annotations a door reads or writes: a unit of a task's item, in that task's project.

An annotations door names a project (`?project=`, whose table holds the rows), a task (`?task=`), a dataset and a unit
key. Each is checked against the task's own record, so a caller cannot write a project's table for a unit no task of
that project holds, nor for a task its role does not reach:

- The project is asked first: someone who may not view it gets D3a's one refusal (`unseen_project`, 404).
- The task must be one they may view, and the project's own: anything else is the task's one refusal (`unseen_task`,
  404), the same words for an absent task, a hidden one and another project's.
- The unit must be one of the task's item (its dataset and one of its keys), else a 404 naming the task and the unit.
- A consensus replica's rows are read only by its own annotator or someone who may review it
  (`tasks.require_work_readable`, 403), so the replicas of an item stay independent.
- A write must also be an edit the task takes now (`tasks.require_editable`): 409 when its state or its project's takes
  none, 403 when the caller lacks the relation or is not who may edit it.

A tag batch names its task the same way, and every unit it tags must be one of that task's (`require_task_units`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query

from annotator.api.dependencies import StoreDep
from annotator.api.security import Checker, CheckerDep, CurrentSubject, ProjectParam, require_view, unseen_project, unseen_task
from annotator.api.v1.endpoints.tasks import require_editable, require_work_readable, viewed_task
from annotator.audit_actions import AnnotatorAuditAction
from annotator.projects.models import Task
from annotator.state.store import AnnotatorStore
from client_kit.media.deps import DatasetParam
from service_kit.exceptions import NotFoundError


#: The task whose item a unit is.
TaskParam = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="The task whose item this unit is")]
#: A unit's key path: `<doc key>/<field>/…`, one segment per identity field (`annotator.annotations.unit`).
UnitKey = Annotated[str, Path(min_length=1, max_length=512, description="The unit's key path, one segment per identity field")]


async def _project_task(store: AnnotatorStore, task: str, project: str, subject: str, checker: Checker) -> Task:
    await require_view(checker, subject, f"project:{project}", unseen_project(project))
    current = await viewed_task(store, task, checker, subject)
    if current.project_id != project:
        raise NotFoundError(unseen_task(task))
    return current


def require_task_units(current: Task, keys: list[str], dataset: str | None) -> None:
    """Each unit key is one of the task's item, in the task's dataset, or the 404 naming the first that is not."""
    for key in keys:
        if current.source.where != dataset or key not in current.source.keys:
            raise NotFoundError(f"unit {key} of dataset {dataset} is not task {current.task_id}'s")


async def _task_unit(store: AnnotatorStore, task: str, project: str, key: str, dataset: str | None, subject: str, checker: Checker) -> Task:
    current = await _project_task(store, task, project, subject, checker)
    require_task_units(current, [key], dataset)
    return current


async def readable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, store: StoreDep, dataset: DatasetParam = None
) -> None:
    """A read of a unit's annotations: the reader may view the project and the task, the unit is the task's, and the
    task's work is the reader's to read (a consensus replica's is its annotator's and its reviewers', `require_work_readable`)."""
    current = await _task_unit(store, task, project, key, dataset, subject, checker)
    await require_work_readable(current, checker, subject)


async def editable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, store: StoreDep, dataset: DatasetParam = None
) -> None:
    """A write of a unit's annotations: a readable unit, and an edit its task takes from this caller now."""
    current = await _task_unit(store, task, project, key, dataset, subject, checker)
    await require_editable(store, current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)


async def editable_task(task: TaskParam, project: ProjectParam, subject: CurrentSubject, checker: CheckerDep, store: StoreDep) -> Task:
    """A write across a task's units (a tag batch): the task is the project's, and takes an edit from this caller now.
    Answers the task's record, against which the route checks every unit it writes (`require_task_units`)."""
    current = await _project_task(store, task, project, subject, checker)
    await require_editable(store, current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)
    return current


EditableTask = Annotated[Task, Depends(editable_task)]
MAY_READ_UNIT = Depends(readable_unit)
MAY_EDIT_UNIT = Depends(editable_unit)
