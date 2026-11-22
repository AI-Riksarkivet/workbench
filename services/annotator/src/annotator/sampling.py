"""Sampling: what orders a project's queue and proposes what to send (ADR 0027, ADR 0034, ADR 0035).

A project's strategy puts first the items its sampling model is least sure about (`least_sure`), the items least like
those already labelled (`least_alike`), or the two interleaved (`mix`). Both numbers are rows of their own beside the
task (`samplings`, `novelties`; `annotator.state.store`), written beside the task and never to it, so sampling moves no
item, claims nothing and changes no draft; a person labels in any order and sends any item.

- A **score** is how unsure a model is of an item: 1 less its confidence. It comes from the score column a pipeline
  wrote into the dataset (`workbench.lancekit.descriptor.SamplingBinding`), or from the model endpoint, asked by the
  workbench's model protocol. A score records the model that gave it: one by another model than the project names now
  is no score.
- A **novelty** is the cosine distance from an item's embedding to the nearest labelled item's, in the vectors the
  dataset declares. It records the model that made those vectors.

Every read of a dataset and every ask of a model happens in a person's own request, since an item is read as that
person. Neither number says anything of a person's labels, so whoever may view an item may read them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

import numpy as np
from pydantic import BaseModel, Field

from annotator.model_endpoint import protocol
from annotator.model_endpoint.client import ModelEndpoint
from annotator.projects.models import AnnotationProject, SampleReason, SamplingStrategy
from annotator.projects.ontology import LabelOntology
from workbench.exceptions import ConflictError


#: Why a project's strategy cannot be followed now, as a person reads it.
SAMPLING_OFF: Final = "sampling is off for this project"
NO_SAMPLING_MODEL: Final = "this project names no model to score its items"
NO_SAMPLING_ENDPOINT: Final = "this deployment names no model endpoint"
NOTHING_MEASURED: Final = "no item is measured against the labelled items yet"

#: The strategies that rank by a model's doubt, and those that rank by how unlike the labelled items an item is.
SCORED: Final = frozenset({SamplingStrategy.LEAST_SURE, SamplingStrategy.MIX})
DIVERSE: Final = frozenset({SamplingStrategy.LEAST_ALIKE, SamplingStrategy.MIX})

#: The most labelled items an item's novelty is measured against: the latest submitted.
MAX_LABELLED: Final = 500
#: The most items one read scores and measures, and the most candidates one proposal ranks.
MAX_READ: Final = 50
MAX_CANDIDATES: Final = 1000
#: How many proposals the annotator keeps for one person on one project: the latest.
KEPT_PROPOSALS: Final = 5


def no_embeddings(dataset: str) -> str:
    """Why the items of ``dataset`` cannot be ranked by diversity."""
    return f"{dataset} declares no embeddings, so diversity is unavailable for its items"


class NoSampling(ConflictError):
    """The project's strategy asks no score of a model (sampling is off), or names no model to ask."""

    problem_type = "about:blank#no-sampling"

    def __init__(self, project_id: str, reason: str) -> None:
        super().__init__(reason, extensions={"project": project_id})


class NoSuchProposal(ConflictError):
    """A send names a proposal that is not one the sender was given for this project."""

    problem_type = "about:blank#no-such-proposal"

    def __init__(self, project_id: str, proposal_id: str) -> None:
        super().__init__(
            f"nothing was sent: {proposal_id} is not a proposal you were given for this project, or it is more than a day old. Ask for the proposal again",
            extensions={"project": project_id},
        )


def why_unavailable(project: AnnotationProject, endpoint: ModelEndpoint, *, scored: int, measured: int) -> str | None:
    """Why ``project``'s strategy orders nothing among the items a listing matches, or None when it orders some:
    sampling is off; it needs a model's score and the project names no model; or none of those items carries a number
    it orders by (``scored`` of them have a score, ``measured`` a novelty), said by what is missing: no endpoint to ask
    for a score, and nothing measured. What a dataset lacks (a score column, embeddings, a labelled item) is said when
    it is read, and an endpoint that fails when it is asked."""
    strategy = project.sampling
    if strategy is SamplingStrategy.OFF:
        return SAMPLING_OFF
    if strategy in SCORED and not project.sampling_model:
        return NO_SAMPLING_MODEL
    no_score = NO_SAMPLING_ENDPOINT if strategy in SCORED and scored == 0 and not endpoint.configured else None
    no_measure = NOTHING_MEASURED if strategy in DIVERSE and measured == 0 else None
    if strategy is SamplingStrategy.MIX:
        return f"{no_score}, and {no_measure}" if no_score and no_measure else None
    return no_score or no_measure


class SampleKind(StrEnum):
    """What a score says of an item: how unsure the model is of it, or that it proposed nothing the item takes."""

    LEAST_SURE = "least_sure"
    PROPOSED_NOTHING = "proposed_nothing"


class Score(BaseModel):
    """A model's score on one item, as a door hands it to the store."""

    task_id: str
    kind: SampleKind
    #: The doubt: 1 less the model's confidence (the highest score among the proposals the item's classes take, or the
    #: dataset's score column). None when it proposed nothing the item takes.
    score: float | None
    model: str
    #: The model's version as its endpoint states it, or the column and table version a score was read from.
    version: str
    #: The person whose request asked the model or read the column.
    asked_by: str


class Sampling(Score):
    """A score as it is kept: when it was recorded, on the database's clock."""

    scored_at: datetime


class Measure(BaseModel):
    """How unlike the project's labelled items one item is, as a door hands it to the store."""

    task_id: str
    #: The cosine distance to the nearest labelled item, from 0 (the same direction) to 2 (the opposite).
    novelty: float
    #: How many labelled items it was measured against.
    against: int
    #: The vector column measured in (`<table>.<column>`), and the model its dataset says made it.
    vectors: str
    model: str | None
    asked_by: str


class Novelty(Measure):
    """A measure as it is kept: when it was recorded, on the database's clock."""

    measured_at: datetime


class ItemSampling(BaseModel):
    """What sampling knows of one listed item: its score by the project's sampling model of now, its novelty, and, in a
    listing ordered by the strategy, which of the two placed it where it is."""

    score: Sampling | None = None
    novelty: Novelty | None = None
    placed_by: SampleReason | None = None


class Proposed(BaseModel):
    """One item a proposal chose, by its unit key: why, the number behind it and the model that number comes from."""

    key: str
    kind: SampleReason
    score: float
    model: str | None


class Proposal(BaseModel):
    """What sampling proposed to one person to send from one dataset. Kept so the send that names it stamps each item
    from what the server chose, never from what a sender says was chosen."""

    proposal_id: str
    project_id: str
    dataset: str
    proposed_by: str
    strategy: SamplingStrategy
    chosen: list[Proposed] = Field(default_factory=list)
    #: What the proposal could not rank by, and why, as a person reads it.
    notes: list[str] = Field(default_factory=list)


def score_answer(answer: protocol.ProposalsResponse, ontology: LabelOntology, *, task_id: str, asked_by: str) -> Score:
    """What ``answer`` says of the item of task ``task_id``: its doubt over the proposals ``ontology`` takes as they are
    (`LabelOntology.takes`, the rule assist keeps proposals by), or that it proposed nothing such."""
    taken = [p.score for p in answer.proposals if ontology.takes(p.label, p.shape_type)]
    if not taken:
        return Score(task_id=task_id, kind=SampleKind.PROPOSED_NOTHING, score=None, model=answer.model, version=answer.version, asked_by=asked_by)
    return Score(task_id=task_id, kind=SampleKind.LEAST_SURE, score=1 - max(taken), model=answer.model, version=answer.version, asked_by=asked_by)


def doubt_of(confidence: float) -> float:
    """The doubt a score column's ``confidence`` states, held to [0, 1] whatever the pipeline wrote."""
    return 1 - min(1.0, max(0.0, confidence))


def _directions(vectors: np.ndarray) -> np.ndarray:
    """Each row scaled to length 1; a row of zeros stays zeros, at distance 1 from everything."""
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(lengths == 0, 1.0, lengths)


def _distances(rows: np.ndarray, others: np.ndarray) -> np.ndarray:
    """The cosine distance from each of ``rows`` to each of ``others``."""
    return 1.0 - _directions(rows) @ _directions(others).T


def novelties(candidates: np.ndarray, labelled: np.ndarray) -> list[float]:
    """Each candidate's cosine distance to the nearest of ``labelled`` (at least one row)."""
    return [float(d) for d in _distances(candidates, labelled).min(axis=1)]


class _Spread:
    """K-center greedy over candidate vectors: each take is the candidate farthest from everything labelled or taken so
    far. With nothing labelled the first take is the candidate farthest from the candidates' own mean."""

    def __init__(self, vectors: np.ndarray, labelled: np.ndarray) -> None:
        self._vectors = vectors
        self._seeded = len(labelled) > 0
        self._nearest = (_distances(vectors, labelled).min(axis=1) if self._seeded else _distances(vectors, vectors.mean(axis=0, keepdims=True))[:, 0]).astype(
            float
        )
        self._open = np.ones(len(vectors), dtype=bool)

    def fold(self, index: int) -> None:
        """Count candidate ``index`` among the taken: the rest are now measured against it too."""
        to_it = _distances(self._vectors, self._vectors[index : index + 1])[:, 0]
        self._nearest = np.minimum(self._nearest, to_it) if self._seeded else to_it
        self._seeded = True
        self._open[index] = False

    def take(self) -> tuple[int, float] | None:
        """The farthest candidate still open and its distance, or None when none is left."""
        if not self._open.any():
            return None
        index = int(np.where(self._open, self._nearest, -np.inf).argmax())
        distance = float(self._nearest[index])
        self.fold(index)
        return index, distance


class Ranking(BaseModel):
    """What a proposal ranks its candidates by: each one's doubt where a score column gives one, each one's vector where
    the dataset declares embeddings, the labelled items' vectors, and the model behind each."""

    keys: list[str]
    doubts: dict[str, float] = Field(default_factory=dict)
    scored_by: str | None = None
    vectors: dict[str, list[float]] = Field(default_factory=dict)
    labelled: list[list[float]] = Field(default_factory=list)
    embedded_by: str | None = None


def propose(ranking: Ranking, lanes: Sequence[SampleReason], count: int) -> list[Proposed]:
    """At most ``count`` of the ranking's candidates, taking from each of ``lanes`` in turn: the highest doubt not yet
    chosen, and the candidate farthest from the labelled and the chosen. A lane with nothing left is passed over."""
    by_doubt = sorted((key for key in ranking.keys if key in ranking.doubts), key=lambda key: -ranking.doubts[key])
    placed = [key for key in ranking.keys if key in ranking.vectors]
    index_of = {key: index for index, key in enumerate(placed)}
    spread = _Spread(np.asarray([ranking.vectors[key] for key in placed], dtype=float), np.asarray(ranking.labelled, dtype=float)) if placed else None
    chosen: dict[str, Proposed] = {}

    def next_by(lane: SampleReason) -> Proposed | None:
        if lane is SampleReason.LEAST_SURE:
            key = next((key for key in by_doubt if key not in chosen), None)
            if key is None:
                return None
            if spread is not None and key in index_of:
                spread.fold(index_of[key])
            return Proposed(key=key, kind=lane, score=ranking.doubts[key], model=ranking.scored_by)
        taken = spread.take() if spread is not None else None
        return None if taken is None else Proposed(key=placed[taken[0]], kind=lane, score=taken[1], model=ranking.embedded_by)

    open_lanes = list(lanes)
    while open_lanes and len(chosen) < count:
        for lane in list(open_lanes):
            if len(chosen) >= count:
                break
            item = next_by(lane)
            if item is None:
                open_lanes.remove(lane)
            else:
                chosen[item.key] = item
    return list(chosen.values())


__all__ = [
    "DIVERSE",
    "KEPT_PROPOSALS",
    "MAX_CANDIDATES",
    "MAX_LABELLED",
    "MAX_READ",
    "NOTHING_MEASURED",
    "NO_SAMPLING_ENDPOINT",
    "NO_SAMPLING_MODEL",
    "SAMPLING_OFF",
    "SCORED",
    "ItemSampling",
    "Measure",
    "NoSampling",
    "NoSuchProposal",
    "Novelty",
    "Proposal",
    "Proposed",
    "Ranking",
    "SampleKind",
    "Sampling",
    "Score",
    "doubt_of",
    "no_embeddings",
    "novelties",
    "propose",
    "score_answer",
    "why_unavailable",
]
