"""Sampling's doors: what a person's request reads and asks so a project's strategy can order its queue and propose
what to send (ADR 0027, ADR 0034, ADR 0035).

- `POST /projects/<id>/sampling/reads` reads, for up to `MAX_READ` of the project's items, what their datasets hold:
  the score column's doubt of each item the project's sampling model has not scored, and each item's distance from the
  labelled ones. It asks no model, and names the items a model is still to be asked about.
- `POST /api/sampling/<unit key>` asks the project's sampling model for its proposals on one item's picture and keeps
  what they say of it as a score; an item that model already scored answers the score kept, and asks no model.
- `POST /projects/<id>/sampling/proposals` proposes which of a dataset's candidate items to send, by the strategy, and
  keeps the proposal; the send that names it (`POST /projects/<id>/items`) stamps each item as sampling's choice or the
  person's.

None of them moves anything: an item's draft, its claim and its task row are left as they are, and the numbers only
order a listing (`GET /projects/<id>/tasks?include=sampling&sort=sampling`).

Every read and every ask is made in a person's own request, never by the annotator on its own: an item is read as that
person, on their own read of the dataset. Scoring and measuring take the annotating relation, so someone who may only
look at a project spends no model and no read on it; a proposal takes the relation a send takes.
"""

import logging
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import UnitKey, ViewedUnit, work_unit
from annotator.annotations.unit import parse_unit
from annotator.api.dependencies import EndpointDep, StoreDep
from annotator.api.security import CAN_ANNOTATE, READS_DATASET, CallerDep, CheckerDep, CurrentSubject, lacking, require_view, unseen_project
from annotator.audit_actions import AnnotatorAuditAction
from annotator.corpus import resolve
from annotator.model_endpoint import protocol
from annotator.model_endpoint.client import ModelEndpointUnavailable, ModelRefusedAsk
from annotator.model_endpoint.picture import picture_of
from annotator.people import RECORDS_PERSON
from annotator.projects.models import AnnotationProject, SampleReason, SamplingStrategy, Task, new_id
from annotator.sampling import (
    DIVERSE,
    MAX_CANDIDATES,
    MAX_LABELLED,
    MAX_READ,
    NO_SAMPLING_ENDPOINT,
    NO_SAMPLING_MODEL,
    SAMPLING_OFF,
    SCORED,
    Measure,
    NoSampling,
    Novelty,
    Proposal,
    Ranking,
    SampleKind,
    Sampling,
    Score,
    no_embeddings,
    propose,
    score_answer,
)
from annotator.sampling_reads import DatasetAnswer, DatasetAsk, Item, confidences, diversity_binding, embeddings, held_units, read_dataset, units_of
from annotator.text import Id, Name
from workbench.exceptions import NotFoundError
from workbench.governed.audit import AuditOutcome, audit
from workbench.governed.token_exchange import LakehouseCaller
from workbench.media.deps import DatasetParam, StateDep
from workbench.media.source_gate import require_read
from workbench.media.state import AppState


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sampling"], dependencies=RECORDS_PERSON)
project_router = APIRouter(prefix="/projects", tags=["sampling"], dependencies=RECORDS_PERSON)

ProjectId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def _ask(state: AppState, caller: LakehouseCaller, dataset: str | None, *, key: str, model: str, task: Task) -> protocol.ProposalsRequest:
    """What the sampling model is asked about the task's item: the unit's picture, read as ``caller``, with no prompt.
    Blocking (the dataset is resolved and a blob read)."""
    corpus = resolve(state, dataset, caller)
    unit = parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    return protocol.ProposalsRequest(
        model=model,
        item=protocol.Item(id=f"{corpus.id}/{unit.key}", content=picture_of(corpus, unit)),
        prompts=protocol.Prompts(),
        classes=[c.name for c in task.ontology.classes][: protocol.MAX_CLASSES],
    )


def _sampling_model(project: AnnotationProject) -> str:
    """The model the project's strategy scores items by, or `NoSampling` when it asks none (sampling is off, or the
    strategy ranks by diversity alone) or names none."""
    if project.sampling not in SCORED:
        raise NoSampling(project.project_id, SAMPLING_OFF)
    if not project.sampling_model:
        raise NoSampling(project.project_id, NO_SAMPLING_MODEL)
    return project.sampling_model


@router.post("/sampling/{key:path}", dependencies=[READS_DATASET])
async def sample(
    task: ViewedUnit,
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    endpoint: EndpointDep,
    checker: CheckerDep,
    subject: CurrentSubject,
    key: UnitKey,
    dataset: DatasetParam = None,
) -> Sampling:
    """Score a task's item by the project's sampling model, or answer the score that model already gave it.

    Asked in this order, so a refusal names no more than its step: 404 unless the caller may view the project and the
    task and the unit is the task's; 403 unless they annotate the project; 409 when the project's strategy asks no score
    or names no model (`NoSampling`); 503 when the deployment names no model endpoint; then the endpoint's own answer,
    each failure the error a person reads (`annotator.model_endpoint.client`).
    """
    obj = f"project:{task.project_id}"
    if not await checker(user=subject, relation=CAN_ANNOTATE, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATOR_SAMPLE, AuditOutcome.DENY, subject=subject, resource=f"task:{task.task_id}", relation=CAN_ANNOTATE)
        raise lacking(subject, CAN_ANNOTATE, obj)
    project = await store.get_project(task.project_id)
    if project is None:
        raise NotFoundError(f"annotation project {task.project_id} does not exist")
    model = _sampling_model(project)
    if (kept := await store.sampling_of(task.task_id, model)) is not None:
        return kept
    if not endpoint.configured:
        raise ModelEndpointUnavailable(NO_SAMPLING_ENDPOINT)
    if task.media.kind != "image":
        raise ModelRefusedAsk(f"a sampling model is shown a picture, and this item is {task.media.kind}")
    ask = await run_in_threadpool(_ask, state, caller, dataset, key=key, model=model, task=task)
    answer = await endpoint.proposals(ask.model_copy(update={"requested_by": subject}))
    score = await store.record_sampling(score_answer(answer, task.ontology, task_id=task.task_id, asked_by=subject))
    audit(
        AnnotatorAuditAction.ANNOTATOR_SAMPLE,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=f"task:{task.task_id}",
        model=answer.model,
        model_version=answer.version,
    )
    logger.info("sampling %s@%s on task %s: %s %s", answer.model, answer.version, task.task_id, score.kind, score.score)
    return score


class ReadsRequest(BaseModel):
    """The items of a project to score from their datasets and measure."""

    task_ids: list[Id] = Field(min_length=1, max_length=MAX_READ)


class SamplingReads(BaseModel):
    """What one read kept: the scores read from score columns, the novelties measured, the items a model is still to be
    asked about (`POST /api/sampling/<unit key>`), and what could not be read and why, as a person reads it."""

    scored: list[Sampling] = Field(default_factory=list)
    measured: list[Novelty] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


async def _annotating(project_id: str, subject: str, checker: CheckerDep, store: StoreDep) -> AnnotationProject:
    """The project, for someone who may view it and annotates it; a project whose sampling is off is refused."""
    obj = f"project:{project_id}"
    await require_view(checker, subject, obj, unseen_project(project_id))
    if not await checker(user=subject, relation=CAN_ANNOTATE, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATOR_SAMPLE, AuditOutcome.DENY, subject=subject, resource=obj, relation=CAN_ANNOTATE)
        raise lacking(subject, CAN_ANNOTATE, obj)
    project = await store.get_project(project_id)
    if project is None:
        raise NotFoundError(unseen_project(project_id))
    if project.sampling is SamplingStrategy.OFF:
        raise NoSampling(project_id, SAMPLING_OFF)
    return project


def _left_unscored(dataset: str, tasks: list[Task], project: AnnotationProject, *, asks: bool) -> tuple[list[str], list[str]]:
    """Of a dataset's items no column scored, those a model is to be asked about, and what is said of the rest."""
    if not tasks:
        return [], []
    if not project.sampling_model:
        return [], [NO_SAMPLING_MODEL]
    if not asks:
        return [], [f"{NO_SAMPLING_ENDPOINT}, so {len(tasks)} item(s) of {dataset} with no score by {project.sampling_model} in their dataset stay unscored"]
    pictured = [task.task_id for task in tasks if task.media.kind == "image"]
    others = len(tasks) - len(pictured)
    return pictured, ([f"a sampling model is shown a picture, so {others} item(s) of {dataset} that are none stay unscored"] if others else [])


@project_router.post("/{project_id}/sampling/reads")
async def read_samples(
    project_id: ProjectId,
    payload: ReadsRequest,
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    endpoint: EndpointDep,
    checker: CheckerDep,
    subject: CurrentSubject,
) -> SamplingReads:
    """Read what the datasets of up to `MAX_READ` of the project's items hold for its strategy, and keep it.

    Asked in this order: 404 unless the caller may view the project; 403 unless they annotate it; 409 when sampling is
    off (`NoSampling`); 403 when they may not read a source the items come from. An id that is not the project's is
    passed over. A dataset that lacks what the strategy ranks by is said in ``notes``, never an error: a project's
    items come from several.
    """
    project = await _annotating(project_id, subject, checker, store)
    tasks = [task for task in await store.get_tasks(project_id, payload.task_ids) if work_unit(task) is not None]
    by_dataset: dict[str, list[Task]] = {}
    for task in tasks:
        if (dataset := task.source.where or state.settings.default_dataset) is not None:
            by_dataset.setdefault(dataset, []).append(task)
    await require_read(state.settings.sources, checker, subject, sorted(by_dataset))
    scores = project.sampling in SCORED
    unscored = {t.task_id for t in tasks} - (await store.scored_among([t.task_id for t in tasks], project.sampling_model) if scores else set())
    answer = SamplingReads()
    for dataset, held in sorted(by_dataset.items()):
        diverse = project.sampling in DIVERSE
        ask = DatasetAsk(
            dataset=dataset,
            model=project.sampling_model if scores else "",
            score=[Item(task_id=t.task_id, key=str(work_unit(t))) for t in held if scores and t.task_id in unscored],
            measure=[Item(task_id=t.task_id, key=str(work_unit(t))) for t in held] if diverse else [],
            labelled=await store.labelled_units(project_id, dataset, default=state.settings.default_dataset, limit=MAX_LABELLED) if diverse else [],
            diverse=diverse,
        )
        read: DatasetAnswer = await run_in_threadpool(read_dataset, state, caller, ask)
        kept = await store.record_samplings(
            [
                Score(task_id=s.task_id, kind=SampleKind.LEAST_SURE, score=s.doubt, model=project.sampling_model, version=s.source, asked_by=subject)
                for s in read.scores
            ]
        )
        answer.scored += kept
        answer.measured += await store.record_novelties([Measure(**m.model_dump(), asked_by=subject) for m in read.measured])
        answer.notes += read.notes
        if scores:
            from_column = {s.task_id for s in read.scores}
            left = [t for t in held if t.task_id in unscored and t.task_id not in from_column]
            to_ask, said = _left_unscored(dataset, left, project, asks=endpoint.configured)
            answer.ask += to_ask
            answer.notes += [note for note in said if note not in answer.notes]
    audit(AnnotatorAuditAction.ANNOTATOR_SAMPLE, AuditOutcome.SUCCESS, subject=subject, resource=f"project:{project_id}")
    logger.info("sampling read on project %s: %d scored, %d measured, %d to ask", project_id, len(answer.scored), len(answer.measured), len(answer.ask))
    return answer


class ProposalRequest(BaseModel):
    """The candidates of one dataset a person has in hand, and how many of them to propose."""

    dataset: Name
    keys: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(min_length=1, max_length=MAX_CANDIDATES)
    count: int = Field(ge=1, le=MAX_CANDIDATES)


def _rank(state: AppState, caller: LakehouseCaller, project: AnnotationProject, ask: ProposalRequest, labelled: list[str]) -> tuple[Ranking, list[str]]:
    """What the dataset gives a proposal to rank ``ask``'s candidates by, read as ``caller``, and what it lacks, as a
    person reads it. Blocking (the dataset is resolved and scanned).

    Least sure is ranked from the dataset's score column only: no model is asked at send. A project that ranks by doubt
    on a dataset with no score by its model ranks by diversity instead, and says so.
    """
    corpus = resolve(state, ask.dataset, caller)
    units = units_of(corpus, list(dict.fromkeys(ask.keys)))
    ranking, notes = Ranking(keys=[unit.key for unit in units]), []
    scored = confidences(corpus, project.sampling_model, units) if project.sampling in SCORED else None
    if scored is not None:
        ranking.doubts, ranking.scored_by = scored[0], project.sampling_model
    elif project.sampling in SCORED:
        why = f"{ask.dataset} carries no score by {project.sampling_model}" if project.sampling_model else NO_SAMPLING_MODEL
        notes.append(f"{why}: least sure orders these items once they are in the project, and they are proposed by how unlike the labelled items they are")
    binding = diversity_binding(corpus)
    if binding is None:
        notes.append(no_embeddings(ask.dataset))
        return ranking, notes
    ranking.vectors = embeddings(corpus, binding, units)
    ranking.labelled = list(embeddings(corpus, binding, held_units(corpus, labelled)).values())
    ranking.embedded_by = binding.model
    return ranking, notes


@project_router.post("/{project_id}/sampling/proposals")
async def propose_items(
    project_id: ProjectId,
    payload: ProposalRequest,
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    checker: CheckerDep,
    subject: CurrentSubject,
) -> Proposal:
    """Propose which of a dataset's candidate items to send to the project, by its strategy, and keep the proposal.

    Asked in this order: 404 unless the caller may view the project; 403 unless they may send items to it and read the
    dataset's source; 409 when sampling is off (`NoSampling`); 422 for a key the dataset cannot name. What the dataset
    lacks for the strategy is said in the proposal's ``notes``; a proposal may choose fewer items than asked, or none.
    """
    obj = f"project:{project_id}"
    await require_view(checker, subject, obj, unseen_project(project_id))
    if not await checker(user=subject, relation="can_send_items", obj=obj):
        audit(AnnotatorAuditAction.ANNOTATOR_SAMPLE, AuditOutcome.DENY, subject=subject, resource=obj, relation="can_send_items")
        raise lacking(subject, "can_send_items", obj)
    await require_read(state.settings.sources, checker, subject, [payload.dataset])
    project = await store.get_project(project_id)
    if project is None:
        raise NotFoundError(unseen_project(project_id))
    if project.sampling is SamplingStrategy.OFF:
        raise NoSampling(project_id, SAMPLING_OFF)
    labelled = await store.labelled_units(project_id, payload.dataset, default=state.settings.default_dataset, limit=MAX_LABELLED)
    ranking, notes = await run_in_threadpool(_rank, state, caller, project, payload, labelled)
    # Doubt first where the dataset gives one; diversity where the strategy asks it, or in doubt's place.
    lanes = [SampleReason.LEAST_SURE] if ranking.scored_by is not None else []
    if project.sampling in DIVERSE or ranking.scored_by is None:
        lanes.append(SampleReason.LEAST_ALIKE)
    proposal = Proposal(
        proposal_id=new_id(),
        project_id=project_id,
        dataset=payload.dataset,
        proposed_by=subject,
        strategy=project.sampling,
        chosen=propose(ranking, lanes, payload.count),
        notes=notes,
    )
    await store.keep_proposal(proposal)
    audit(AnnotatorAuditAction.ANNOTATOR_SAMPLE, AuditOutcome.SUCCESS, subject=subject, resource=obj)
    return proposal
