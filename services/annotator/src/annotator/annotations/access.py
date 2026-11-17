"""Whose work an annotations door reads or writes: the unit of a task's item, in that task's project.

An annotations door names a project (`?project=`), a task (`?task=`, whose draft holds the work), a dataset and a unit
key. Each is checked against the task's own record, so a caller cannot read or write a task's work through a unit the
task does not label, nor for a task its role does not reach:

- The project is asked first: someone who may not view it gets D3a's one refusal (`unseen_project`, 404).
- The task must be one they may view, and the project's own: anything else is the task's one refusal (`unseen_task`,
  404), the same words for an absent task, a hidden one and another project's.
- The unit must be the one the task's work is of: its item's first key, in its dataset (`work_unit`; a task's draft holds
  one unit's shapes), else a 404 naming the task and the unit.
- A consensus replica's rows are read only by its own annotator or someone who may review it
  (`tasks.require_work_readable`, 403), so the replicas of an item stay independent.
- A write must also be an edit the task takes now (`tasks.require_editable`): 409 when its state or its project's takes
  none, 403 when the caller lacks the relation or is not who may edit it.

A tag batch names its task the same way, and every unit it tags must be that task's (`require_task_units`).
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


def work_unit(task: Task) -> str | None:
    """The unit whose work the task's draft holds: its item's first key, or None for an item that names no unit."""
    return task.source.keys[0] if task.source.keys else None


def require_task_units(current: Task, keys: list[str], dataset: str | None) -> None:
    """Each unit key is the task's work unit, in the task's dataset, or the 404 naming the first that is not."""
    unit = work_unit(current)
    for key in keys:
        if current.source.where != dataset or key != unit:
            raise NotFoundError(f"unit {key} of dataset {dataset} is not task {current.task_id}'s")


async def _task_unit(store: AnnotatorStore, task: str, project: str, key: str, dataset: str | None, subject: str, checker: Checker) -> Task:
    current = await _project_task(store, task, project, subject, checker)
    require_task_units(current, [key], dataset)
    return current


async def readable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, store: StoreDep, dataset: DatasetParam = None
) -> Task:
    """A read of a unit's work: the reader may view the project and the task, the unit is the task's, and the task's
    work is the reader's to read (a consensus replica's is its annotator's and its reviewers', `require_work_readable`).
    Answers the task's record."""
    current = await _task_unit(store, task, project, key, dataset, subject, checker)
    await require_work_readable(current, checker, subject)
    return current


async def editable_unit(
    task: TaskParam, project: ProjectParam, key: UnitKey, subject: CurrentSubject, checker: CheckerDep, store: StoreDep, dataset: DatasetParam = None
) -> Task:
    """A write of a unit's work: a readable unit, and an edit its task takes from this caller now. Answers the task's
    record, on which the store checks the same rules again under the task's row lock."""
    current = await _task_unit(store, task, project, key, dataset, subject, checker)
    await require_editable(store, current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)
    return current


async def editable_task(task: TaskParam, project: ProjectParam, subject: CurrentSubject, checker: CheckerDep, store: StoreDep) -> Task:
    """A write across a task's units (a tag batch): the task is the project's, and takes an edit from this caller now.
    Answers the task's record, against which the route checks every unit it writes (`require_task_units`)."""
    current = await _project_task(store, task, project, subject, checker)
    await require_editable(store, current, checker, subject, AnnotatorAuditAction.ANNOTATION_SAVE)
    return current


EditableTask = Annotated[Task, Depends(editable_task)]
ReadableUnit = Annotated[Task, Depends(readable_unit)]
EditableUnit = Annotated[Task, Depends(editable_unit)]
