"""A model's output is normalized and checked against the TASK — at the boundary, not by a human.

Two holes met here, and the owner named the second one: "schemas must align from the ml backend".

1. `AssistShape.shape_type` defaulted to `"rectangle"` and was a free string. That name is accepted
   by neither the service vocabulary (`bbox`) nor the canvas, so a prediction arrived speaking a
   dialect nothing downstream understood — the same engine/service split that stopped five of seven
   drawing tools from submitting.
2. NOTHING checked a prediction against the task's own contract. A producer returning a polygon for
   a bbox-only task put that shape in the review queue, and it was refused at SUBMIT — after a human
   had already looked at it. The mismatch was real; it was just deferred onto the annotator.

A producer is not obliged to know the task's rules; the registry exists so a backend is a config
entry. So both are resolved once, server-side, where the task's captured ontology lives.
"""

from __future__ import annotations

import pytest

from annotator.api.v1.endpoints.assist import _CANONICAL_SHAPE, AssistShape, _task_ontology, _within_contract
from annotator.core.config import AnnotatorSettings
from annotator.projects.ontology import LabelClass, LabelOntology
from annotator.state.store import AnnotatorStore


def _shape(shape_type: str) -> AssistShape:
    return AssistShape(shape_type=shape_type, x=0, y=0, width=10, height=10)


@pytest.mark.parametrize(
    ("produced", "canonical"),
    [
        ("box", "bbox"),
    ],
)
def test_a_producers_dialect_is_normalized_to_the_canonical_vocabulary(produced: str, canonical: str) -> None:
    assert _CANONICAL_SHAPE[produced] == canonical


def test_an_ontology_that_CONSTRAINS_NOTHING_filters_nothing() -> None:
    """An unconstrained class voids the tool rule, so there is no rule to drop a prediction for.
    Filtering here would discard work the server would happily have accepted at submit."""
    ontology = LabelOntology(kind="object-detection", classes=[LabelClass(name="anything")])

    kept, dropped = _within_contract([_shape("polygon")], ontology)

    assert len(kept) == 1
    assert dropped == []


@pytest.mark.asyncio
async def test_a_TRANSPORT_failure_reaching_the_task_still_returns_the_predictions(state_settings: AnnotatorSettings) -> None:
    """An UNREACHABLE store is not an unreadable rule, and only the first one is tolerated here.

    An ontology the current model cannot PARSE raises 409 out of `enforced_shape_types`: an unreadable rule is not the
    absence of a rule. What this exercises is the TRANSPORT path: the store does not answer (its pool is closed), so
    there is no ontology to have an opinion about. This is not a gate — submit still refuses a violating shape — and
    losing a real prediction to a momentary outage would be invisible to the annotator, who would simply see fewer
    suggestions.
    """
    store = await AnnotatorStore.open(state_settings)
    await store.close()

    ontology = await _task_ontology(store, "p1", "t1")
    kept, dropped = _within_contract([_shape("polygon")], ontology)

    assert len(kept) == 1, "a prediction was lost to a momentary store outage"
    assert dropped == []
