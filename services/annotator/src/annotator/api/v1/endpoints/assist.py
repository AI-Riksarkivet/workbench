"""Assist: asking a model, from the canvas, for proposals on the open item.

The annotator calls the deployment's model endpoint by the workbench's model protocol (`annotator.model_endpoint`,
ADR 0023 and 0030) and deploys no model. `GET /api/assist/models` says which models the endpoint serves and whether
each fits the task; `POST /api/assist/<unit key>` sends one of them the unit's picture with the person's prompts and
answers its proposals, each with the model, its version and its score. A deployment that names no endpoint says assist
is unavailable, and an endpoint that is slow or fails is an error the person reads, never an empty answer.

A proposal is a person's to settle: the canvas saves it as a `prediction` row, and the item cannot be submitted until
each is kept or removed (ADR 0019, `annotator.state.store`).
"""

import base64
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from annotator.annotations.access import UnitKey, require_task_units
from annotator.annotations.unit import Unit, parse_unit
from annotator.api.dependencies import StoreDep
from annotator.api.security import READS_DATASET, CallerDep, CheckerDep, CurrentSubject, ProjectParam, lacking, unseen_task
from annotator.audit_actions import AnnotatorAuditAction
from annotator.core.config import AnnotatorSettings
from annotator.corpus import Corpus, resolve
from annotator.model_endpoint import protocol
from annotator.model_endpoint.client import NO_ENDPOINT, ModelEndpoint, ModelEndpointUnavailable, ModelRefusedAsk
from annotator.projects.models import ShapeType, Task
from annotator.projects.ontology import LabelOntology
from annotator.text import Text
from workbench.exceptions import NotFoundError
from workbench.governed.audit import AuditOutcome, audit
from workbench.lancekit.registry import table_dataset
from workbench.media.deps import DatasetParam, StateDep
from workbench.media.frames import frame_row


logger = logging.getLogger(__name__)

#: A picture's media type as the protocol carries it (`protocol.ImageContent.mime`).
IMAGE_TYPE = re.compile(protocol.IMAGE_TYPE_PATTERN)

#: A proposal is a proposed write, so asking for one takes the annotating relation on the project the request names:
#: someone who may only look at a project spends no model on it.
ANNOTATE = "can_annotate"


async def require_assist(subject: CurrentSubject, checker: CheckerDep, project: ProjectParam) -> None:
    """The door of every assist route: the caller annotates the project the request names (`?project=`)."""
    obj = f"project:{project}"
    if not await checker(user=subject, relation=ANNOTATE, obj=obj):
        audit(AnnotatorAuditAction.ANNOTATOR_ASSIST, AuditOutcome.DENY, subject=subject, resource=obj, relation=ANNOTATE)
        raise lacking(subject, ANNOTATE, obj)


router = APIRouter(prefix="/api", tags=["assist"], dependencies=[Depends(require_assist)])


def _endpoint(state: StateDep) -> ModelEndpoint:
    settings = state.settings
    if not isinstance(settings, AnnotatorSettings):
        raise TypeError(f"assist reads the annotator's settings, not {type(settings).__name__}")
    return settings.model_endpoint


EndpointDep = Annotated[ModelEndpoint, Depends(_endpoint)]

FINITE = ConfigDict(allow_inf_nan=False)


class Region(BaseModel):
    """A box the person drew on the picture, in its pixels."""

    model_config = FINITE

    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class Point(BaseModel):
    """A click on the picture, in its pixels. ``positive: false`` says "not this"."""

    model_config = FINITE

    x: float
    y: float
    positive: bool = True


class AssistRequest(BaseModel):
    """What to ask: the model, and what the person added (words, a drawn box, the clicks of the session so far)."""

    model_config = ConfigDict(extra="forbid")

    model: protocol.Name
    prompt: Annotated[Text, Field(max_length=protocol.MAX_PROMPT)] | None = None
    region: Region | None = None
    points: list[Point] = Field(default_factory=list, max_length=protocol.MAX_POINTS)
    #: The task the ask is for. The unit must be its item's, and the annotator reads its classes itself: a client never
    #: sends the rules it is judged by.
    task_id: Annotated[Text, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class AssistModel(BaseModel):
    """One model the task can ask, as the endpoint describes it."""

    name: str
    version: str
    title: str
    reads: list[protocol.ContentKind]
    prompts: list[protocol.PromptKind]
    returns: list[ShapeType]
    #: Whether what it returns fits the task named in `?task_id=`: `null` when no task is named or its classes hold
    #: their shapes to no type, so there is nothing to judge it against.
    compatible: bool | None


class AssistModels(BaseModel):
    """The models a task can ask. ``available: false`` is a deployment with no model endpoint, with the reason a person
    is shown; the endpoint's address is never part of the answer."""

    available: bool
    reason: str
    models: list[AssistModel]


class AssistProposal(BaseModel):
    """One proposed shape, in the picture's pixels, with the model's score."""

    shape_type: ShapeType
    x: float
    y: float
    width: float
    height: float
    polygon: list[float]
    label: str
    text: str
    score: float


class AssistAnswer(BaseModel):
    """A model's proposals: which model answered, at which version, what it proposes, and what the task's own classes
    refused of its answer, said rather than dropped in silence."""

    model: str
    version: str
    proposals: list[AssistProposal]
    dropped: list[str]


async def _task(store: StoreDep, project: str, task_id: str) -> Task:
    """The task, held to the project the door checked: another project's task reads as one that does not exist."""
    task = await store.get_project_task(project, task_id)
    if task is None:
        raise NotFoundError(unseen_task(task_id))
    return task


def _allowed_shapes(ontology: LabelOntology) -> set[str]:
    """The shape types the task's classes take; empty when they hold shapes to no type."""
    return set(ontology.tools) if ontology.constrains else set()


@router.get("/assist/models")
async def models(store: StoreDep, endpoint: EndpointDep, project: ProjectParam, task_id: str | None = None) -> AssistModels:
    """The models the deployment's endpoint serves, and whether each fits the task."""
    if not endpoint.configured:
        return AssistModels(available=False, reason=NO_ENDPOINT, models=[])
    allowed = _allowed_shapes((await _task(store, project, task_id)).ontology) if task_id else set()
    listing = await endpoint.models()
    return AssistModels(
        available=True,
        reason="",
        models=[
            AssistModel(
                name=model.name,
                version=model.version,
                title=model.title,
                reads=model.reads,
                prompts=model.prompts,
                returns=list(model.returns),
                compatible=bool(set(model.returns) & allowed) if allowed else None,
            )
            for model in listing.models
        ],
    )


def _picture(corpus: Corpus, unit: Unit) -> protocol.ImageContent:
    """The unit's picture, the one the canvas draws: its frame, or its document's thumbnail when the unit is a whole
    document. Blocking (a scan and a blob read)."""
    handle, declared = corpus.handle, corpus.declared
    doc, *rest = unit.values.values()
    if rest:
        if not all(isinstance(value, int) for value in rest):
            raise ModelRefusedAsk("assist shows a model a unit's frame, and this unit's key names none")
        frame = frame_row(handle, str(doc), tuple(int(value) for value in rest), 0)
        ds, column, row, mime = frame.ds, frame.column, frame.row, frame.mime
    else:
        binding = declared.document
        if binding is None or binding.thumbnail is None:
            raise NotFoundError("this item has no picture a model can be shown")
        ds, column = table_dataset(handle, binding.table), binding.thumbnail
        columns = [binding.thumbnail_mime] if binding.thumbnail_mime else []
        found = ds.to_table(columns=columns, filter=unit.where, with_row_id=True, limit=1)
        if found.num_rows == 0:
            raise NotFoundError("this item has no picture a model can be shown")
        row = int(found.column("_rowid")[0].as_py())
        mime = (found.column(binding.thumbnail_mime)[0].as_py() if binding.thumbnail_mime else None) or "image/jpeg"
    # The stored type without its parameters (`image/jpeg; charset=binary`), held to what the protocol carries.
    mime = mime.partition(";")[0].strip().lower()
    if not IMAGE_TYPE.fullmatch(mime):
        raise ModelRefusedAsk(f"assist shows a model a picture, and this item's is {mime[:80]!r}")
    blob = ds.take_blobs(column, ids=[row])[0]
    if blob is None:
        raise NotFoundError("this item has no picture a model can be shown")
    with blob as payload:
        size = payload.size()
        if size > protocol.MAX_IMAGE_BYTES:
            raise ModelRefusedAsk(f"this item's picture is {size} bytes, more than the {protocol.MAX_IMAGE_BYTES} a model is sent")
        data = payload.read()
    return protocol.ImageContent(mime=mime, data=base64.b64encode(data).decode("ascii"))


def _within_the_task(proposals: list[protocol.Proposal], ontology: LabelOntology) -> tuple[list[AssistProposal], list[str]]:
    """The proposals the task's classes can take, and what was refused of the rest.

    A model is not obliged to know a task's rules, so the mismatch is settled here, once: a shape of a type no class of
    the task takes is refused and said, and a label that is none of the task's classes is not carried (the person picks
    the class), since a submit would refuse either.
    """
    allowed = _allowed_shapes(ontology)
    classes = {c.name for c in ontology.classes}
    kept: list[AssistProposal] = []
    refused: dict[str, int] = {}
    for proposal in proposals:
        if allowed and proposal.shape_type not in allowed:
            refused[proposal.shape_type] = refused.get(proposal.shape_type, 0) + 1
            continue
        label = proposal.label if not classes or proposal.label in classes else ""
        kept.append(AssistProposal(**proposal.model_dump(include=set(AssistProposal.model_fields) - {"label"}), label=label))
    dropped = [f"{count} {shape} proposal(s): this item's classes take {', '.join(sorted(allowed))}" for shape, count in sorted(refused.items())]
    return kept, dropped


@router.post("/assist/{key:path}", dependencies=[READS_DATASET])
async def assist(
    caller: CallerDep,
    state: StateDep,
    store: StoreDep,
    endpoint: EndpointDep,
    subject: CurrentSubject,
    project: ProjectParam,
    key: UnitKey,
    body: AssistRequest,
    dataset: DatasetParam = None,
) -> AssistAnswer:
    """Ask one model for proposals on a task's unit.

    The unit is named as the annotations doors name it (its key path, `annotator.annotations.unit`) and must be the
    named task's, so a person who annotates one item spends no model on another. The dataset is read as the caller.
    """
    task = await _task(store, project, body.task_id)
    require_task_units(task, [key], dataset)
    if not endpoint.configured:
        # Said before any read: a deployment with no endpoint answers the same for every item.
        raise ModelEndpointUnavailable
    if task.media.kind != "image":
        raise ModelRefusedAsk(f"assist shows a model a picture, and this item is {task.media.kind}")
    corpus = await run_in_threadpool(resolve, state, dataset, caller)
    unit = parse_unit(corpus.declared, corpus.handle.descriptor.tables, key)
    picture = await run_in_threadpool(_picture, corpus, unit)
    ask = protocol.ProposalsRequest(
        model=body.model,
        item=protocol.Item(id=f"{corpus.id}/{unit.key}", content=picture),
        prompts=protocol.Prompts(
            text=(body.prompt or "").strip() or None,
            box=protocol.Box(**body.region.model_dump()) if body.region else None,
            points=[protocol.Point(**point.model_dump()) for point in body.points],
        ),
        classes=[c.name for c in task.ontology.classes][: protocol.MAX_CLASSES],
        requested_by=subject,
    )
    answer = await endpoint.proposals(ask)
    proposals, dropped = _within_the_task(answer.proposals, task.ontology)
    audit(
        AnnotatorAuditAction.ANNOTATOR_ASSIST,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=f"task:{task.task_id}",
        model=answer.model,
        model_version=answer.version,
    )
    # A prompt is a person's free text and is never logged; its length is.
    logger.info(
        "assist %s@%s (prompt %d chars) → %d proposal(s), %d refused", answer.model, answer.version, len(body.prompt or ""), len(proposals), len(dropped)
    )
    return AssistAnswer(model=answer.model, version=answer.version, proposals=proposals, dropped=dropped)
