"""The judge: asking the model a project names what it disagrees with among the labels of a submitted item (ADR 0026).

`POST /api/judge/<unit key>` judges one item in review and keeps the verdict (`annotator.judgement`); an item already
judged answers the verdict kept, and asks no model. It moves nothing: a reviewer accepts or sends back every item, and
the verdict only orders and marks their list (`GET /projects/<id>/tasks?include=judge&sort=judge`).

The judge is asked in a person's own request, never by the annotator on its own: the item's picture is read as that
person, on their own read of the dataset, and the annotator holds no credential that reads it for nobody. The person is
the one who submitted the item, or someone who may review it; the app asks as each when a submit lands and when a
reviewer opens their list.
"""

import logging

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import ReadableUnit, UnitKey
from annotator.annotations.unit import parse_unit
from annotator.api.dependencies import EndpointDep, StoreDep
from annotator.api.security import CAN_REVIEW, READS_DATASET, CallerDep, CheckerDep, CurrentSubject, lacking
from annotator.audit_actions import AnnotatorAuditAction
from annotator.corpus import resolve
from annotator.judgement import Flag, Judgement, NoJudge, Verdict, labels_asked
from annotator.model_endpoint import protocol
from annotator.model_endpoint.client import ModelEndpointUnavailable, ModelRefusedAsk
from annotator.model_endpoint.picture import picture_of
from annotator.people import RECORDS_PERSON
from annotator.projects.models import Draft, Task, TaskState
from workbench.exceptions import ConflictError
from workbench.governed.audit import AuditOutcome, audit
from workbench.governed.token_exchange import LakehouseCaller
from workbench.media.deps import DatasetParam, StateDep
from workbench.media.state import AppState


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["judge"], dependencies=RECORDS_PERSON)


def _ask(state: AppState, caller: LakehouseCaller, dataset: str | None, key: str, judge: str, task: Task, draft: Draft | None) -> protocol.AssessmentRequest:
    """What the judge is asked about the task's submission: the unit's picture, read as ``caller``, and the labels of
    its work. Blocking (the dataset is resolved and a blob read)."""
    corpus = resolve(state, dataset, caller)
    unit = parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    return protocol.AssessmentRequest(
        model=judge,
        item=protocol.Item(id=f"{corpus.id}/{unit.key}", content=picture_of(corpus, unit)),
        labels=labels_asked(draft)[: protocol.MAX_LABELS],
        classes=[c.name for c in task.ontology.classes][: protocol.MAX_CLASSES],
    )


def _takeable(suggested: str, label_id: str | None, task: Task, draft: Draft | None) -> str:
    """``suggested`` where the task's classes let the flagged label's shape take it, else nothing: a model is told the
    classes but not which shapes each takes, so it can suggest one the label cannot carry, which a reviewer could never
    choose. The rule is the one assist keeps a proposal's class by (`LabelOntology.class_takes`). A flag on the whole
    item names no label, and its suggestion is kept as it is."""
    if not suggested or label_id is None:
        return suggested
    shape = next((s for s in (draft.shapes if draft else []) if s.shape_id == label_id), None)
    return suggested if shape is not None and task.ontology.class_takes(suggested, shape.shape_type) else ""


@router.post("/judge/{key:path}", dependencies=[READS_DATASET])
async def judge(
    task: ReadableUnit,
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    endpoint: EndpointDep,
    checker: CheckerDep,
    subject: CurrentSubject,
    key: UnitKey,
    dataset: DatasetParam = None,
) -> Judgement:
    """Judge a task's submission, or answer the verdict it already has.

    Asked in this order, so a refusal names no more than its step: 403 unless the caller submitted the item or may
    review it; 409 when the item is not in review, or its project names no judge (`NoJudge`); 503 when the deployment
    names no model endpoint; then the endpoint's own answer, each failure the error a person reads
    (`annotator.model_endpoint.client`).
    """
    obj = f"project:{task.project_id}"
    if task.submitted_by != subject and not await checker(user=subject, relation=CAN_REVIEW, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATOR_JUDGE, AuditOutcome.DENY, subject=subject, resource=f"task:{task.task_id}", relation=CAN_REVIEW)
        raise lacking(subject, CAN_REVIEW, obj)
    if task.state is not TaskState.IN_REVIEW or task.submitted_at is None:
        raise ConflictError(f"this item is {task.state}: a judge reads an item in review", extensions={"task": task.task_id})
    if (kept := await store.judgement_of(task.task_id, task.submitted_at)) is not None:
        return kept
    project = await store.get_project(task.project_id)
    if project is None or not project.judge_model:
        raise NoJudge(task.project_id)
    if not endpoint.configured:
        raise ModelEndpointUnavailable
    if task.media.kind != "image":
        raise ModelRefusedAsk(f"a judge is shown a picture, and this item is {task.media.kind}")
    draft = await store.get_draft(task.task_id)
    ask = await run_in_threadpool(_ask, state, caller, dataset, key, project.judge_model, task, draft)
    answer = await endpoint.assessment(ask.model_copy(update={"requested_by": subject}))
    verdict = await store.record_judgement(
        Verdict(
            task_id=task.task_id,
            submitted_at=task.submitted_at,
            revision=draft.revision if draft else 0,
            model=answer.model,
            version=answer.version,
            asked_by=subject,
            flags=[
                Flag(shape_id=d.label_id, reason=d.reason, suggested_label=_takeable(d.suggested_label, d.label_id, task, draft), score=d.score)
                for d in answer.disagreements
            ],
        )
    )
    audit(
        AnnotatorAuditAction.ANNOTATOR_JUDGE,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=f"task:{task.task_id}",
        model=answer.model,
        model_version=answer.version,
    )
    logger.info("judge %s@%s on task %s: %d flag(s)", answer.model, answer.version, task.task_id, len(verdict.flags))
    return verdict
