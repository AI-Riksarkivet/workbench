"""What sampling reads from a dataset, as the person whose request it is (ADR 0034): the score column a pipeline wrote
and the embeddings the dataset declares, by unit key, through the caller's own handle on the dataset.

Every function here is blocking (a dataset is resolved and its tables scanned), so an async door runs it in the
threadpool. Each scan names its rows by key and reads two kinds of column, the identity fields and the one asked for,
`KEYS_PER_SCAN` rows a scan.
"""

from __future__ import annotations

import math
from itertools import batched
from typing import TYPE_CHECKING, Final

import numpy as np
from pydantic import BaseModel, Field

from annotator.annotations.unit import Unit, UnitKeyError, parse_unit
from annotator.corpus import Corpus, resolve
from annotator.sampling import doubt_of, no_embeddings, novelties
from workbench.exceptions import ConflictError, ForbiddenError, NotFoundError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.descriptor import EmbeddingBinding
from workbench.lancekit.predicate import or_
from workbench.lancekit.registry import table_dataset


if TYPE_CHECKING:
    from workbench.media.state import AppState


#: How many rows one scan names in its filter.
KEYS_PER_SCAN: Final = 100


class Item(BaseModel):
    """One of a project's items, as a read names it: its task and its unit's key in the dataset."""

    task_id: str
    key: str


class DatasetAsk(BaseModel):
    """What one read asks of one dataset: the items to score from its score column by ``model``, the items to measure,
    and the labelled items' unit keys to measure them against."""

    dataset: str
    model: str = ""
    score: list[Item] = Field(default_factory=list)
    measure: list[Item] = Field(default_factory=list)
    labelled: list[str] = Field(default_factory=list)
    #: Whether the project's strategy ranks by diversity: a dataset that cannot be is then said.
    diverse: bool = False


class ColumnScore(BaseModel):
    """The doubt a dataset's score column states of one item, and the column it was read from."""

    task_id: str
    doubt: float
    source: str


class Measured(BaseModel):
    """One item's distance to the nearest labelled item, and what it was measured in."""

    task_id: str
    novelty: float
    against: int
    vectors: str
    model: str | None


class DatasetAnswer(BaseModel):
    """What a dataset gave one read, and what it could not give, as a person reads it."""

    scores: list[ColumnScore] = Field(default_factory=list)
    measured: list[Measured] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def units_of(corpus: Corpus, keys: list[str]) -> list[Unit]:
    """Each of ``keys`` read against the dataset's identity.

    Raises:
        UnitKeyError: A key the dataset's identity cannot name.
    """
    return [parse_unit(corpus.declared, corpus.handle.descriptor.tables, key) for key in keys]


def _cells(corpus: Corpus, table: str, column: str, units: list[Unit]) -> tuple[dict[str, object], int]:
    """``column`` of each of ``units``' rows in ``table`` that holds a value, by unit key, and the table's version."""
    ds = table_dataset(corpus.handle, table)
    found: dict[str, object] = {}
    for chunk in batched(units, KEYS_PER_SCAN, strict=False):
        key_of = {tuple(unit.values.values()): unit.key for unit in chunk}
        fields = list(chunk[0].values)
        rows = ds.to_table(columns=[*fields, column], filter=or_(*(unit.where for unit in chunk))).to_pylist()
        for row in rows:
            key = key_of.get(tuple(row[field] for field in fields))
            if key is not None and row[column] is not None:
                found[key] = row[column]
    return found, int(ds.version)


def diversity_binding(corpus: Corpus) -> EmbeddingBinding | None:
    """The vectors a dataset's items are compared in (`Declared.embeddings`), where their table names a row by the
    dataset's identity. None for a dataset that declares none."""
    binding = corpus.declared.embeddings
    info = corpus.handle.descriptor.tables.get(binding.table) if binding else None
    if binding is None or info is None or any(info.column(field) is None for field in corpus.declared.identity.key_fields):
        return None
    return binding


def confidences(corpus: Corpus, model: str, units: list[Unit]) -> tuple[dict[str, float], str] | None:
    """The doubt the dataset's score column states of each of ``units`` it scores, by unit key, and the column read
    (`<table>.<column> at version <n>`); None when the dataset declares no score column by ``model``."""
    binding = corpus.declared.sampling.scores if corpus.declared.sampling else None
    if binding is None or not model or binding.model != model:
        return None
    if not units:
        return {}, f"{binding.table}.{binding.column}"
    cells, version = _cells(corpus, binding.table, binding.column, units)
    # A cell that is no number (a pipeline's NaN) scores nothing, as a null does.
    doubts = {key: doubt_of(value) for key, value in cells.items() if isinstance(value, float) and math.isfinite(value)}
    return doubts, f"{binding.table}.{binding.column} at version {version}"


def embeddings(corpus: Corpus, binding: EmbeddingBinding, units: list[Unit]) -> dict[str, list[float]]:
    """The embedding of each of ``units`` that has one, by unit key; a vector holding a value that is no number is none."""
    if not units:
        return {}
    cells, _version = _cells(corpus, binding.table, binding.column, units)
    return {key: value for key, value in cells.items() if isinstance(value, list) and all(isinstance(x, float) and math.isfinite(x) for x in value)}


def held_units(corpus: Corpus, keys: list[str]) -> list[Unit]:
    """The units of keys the annotator itself holds (its items' own); one the dataset cannot name is left out, never
    refused: the person asking did not send it."""
    return list(_parsed(corpus, [Item(task_id=key, key=key) for key in keys]).values())


def _parsed(corpus: Corpus, items: list[Item]) -> dict[str, Unit]:
    """Each item's unit by task id; an item whose key the dataset cannot name is left out."""
    units: dict[str, Unit] = {}
    for item in items:
        try:
            units[item.task_id] = units_of(corpus, [item.key])[0]
        except UnitKeyError:
            continue
    return units


def _measure(corpus: Corpus, ask: DatasetAsk, answer: DatasetAnswer) -> None:
    binding = diversity_binding(corpus)
    if binding is None:
        answer.notes.append(no_embeddings(ask.dataset))
        return
    units = _parsed(corpus, ask.measure)
    held = embeddings(corpus, binding, held_units(corpus, ask.labelled))
    if not held:
        answer.notes.append(f"no item of {ask.dataset} is labelled yet, so there is nothing to measure its items against")
        return
    found = embeddings(corpus, binding, list(units.values()))
    for task_id, unit in units.items():
        if unit.key not in found:
            continue
        # An item is not measured against itself.
        others = [vector for key, vector in held.items() if key != unit.key]
        if not others:
            continue
        distance = novelties(np.asarray([found[unit.key]], dtype=float), np.asarray(others, dtype=float))[0]
        answer.measured.append(
            Measured(task_id=task_id, novelty=distance, against=len(others), vectors=f"{binding.table}.{binding.column}", model=binding.model)
        )


def read_dataset(state: AppState, caller: LakehouseCaller, ask: DatasetAsk) -> DatasetAnswer:
    """What ``ask.dataset`` gives sampling, read as ``caller``: the score column's doubt of each item to score, and each
    item to measure's distance from the labelled ones. A dataset the caller cannot read, or that cannot be annotated,
    gives nothing and says so."""
    answer = DatasetAnswer()
    try:
        corpus = resolve(state, ask.dataset, caller)
    except (NotFoundError, ForbiddenError, ConflictError) as exc:
        answer.notes.append(f"{ask.dataset} could not be read: {exc}")
        return answer
    to_score = _parsed(corpus, ask.score)
    scored = confidences(corpus, ask.model, list(to_score.values()))
    if scored is not None:
        doubts, source = scored
        answer.scores = [ColumnScore(task_id=task_id, doubt=doubts[unit.key], source=source) for task_id, unit in to_score.items() if unit.key in doubts]
    if ask.diverse:
        _measure(corpus, ask, answer)
    return answer


__all__ = [
    "ColumnScore",
    "DatasetAnswer",
    "DatasetAsk",
    "Item",
    "Measured",
    "confidences",
    "diversity_binding",
    "embeddings",
    "held_units",
    "read_dataset",
    "units_of",
]
