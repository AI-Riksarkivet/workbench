"""The settings surface: which producers exist, real or mocked, and what they return.

The owner asked "why is there no settings menu for which endpoints or runner is configured.. and
what they return". There was none — and the one list that DID exist was the annotator ZONE
re-parsing `MEDIA_ASSIST_BACKENDS` out of its own web pod's env. Two copies of one registry, and
`config.remote.ts` said so in a comment ("even when this web pod's env drifts from the service's").
A service pointed at a real model server could therefore be presented as mocked, and vice versa.

The service is the process that resolves and calls a backend, so it is the only source that cannot
be wrong. These tests pin what it reports and — more importantly — what it refuses to claim.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from annotator.api.v1.endpoints.assist import enforced_shape_types, producer_listing, returns_for
from annotator.core.config import AnnotatorSettings
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, Task
from annotator.projects.ontology import LabelOntology
from annotator.state.store import AnnotatorStore


def _settings(backends: dict[str, str] | None = None, default: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(assist_backends=backends or {}, assist_url=default)


def _by_name(listing) -> dict:  # noqa: ANN001 - a pydantic model, named for readability
    return {p.name: p for p in listing.producers}


@pytest.mark.asyncio
async def test_an_unconfigured_service_still_lists_the_mock_producers() -> None:
    """An EMPTY settings surface would read as "assist is unavailable" — but both interactive loops
    work against the in-repo mock. The honest report is "present, and mocked"."""
    rows = _by_name(producer_listing(_settings()))

    # The FULL family — the batch producers included (§9.5: one registry, not two truths).
    assert {"grounding-dino", "sam", "htr", "insid3", "vlm-judge", "embed-propagate"} <= set(rows)
    assert not rows["grounding-dino"].configured
    assert not rows["sam"].configured


@pytest.mark.asyncio
async def test_a_registered_producer_reports_configured_without_leaking_its_url() -> None:
    listing = producer_listing(_settings({"sam": "http://sam-internal:8000"}))
    rows = _by_name(listing)

    assert rows["sam"].configured
    assert not rows["grounding-dino"].configured, "an unregistered name was reported as real"
    assert listing.urls_redacted
    # Any authenticated annotator can call this; an internal model-server URL is not theirs to have.
    assert "sam-internal" not in listing.model_dump_json()


@pytest.mark.asyncio
async def test_the_default_url_makes_EVERY_producer_configured() -> None:
    """`assist_url` is the catch-all, so with one set nothing falls through to the mock."""
    listing = producer_listing(_settings(default="http://models:9000"))

    assert listing.default_configured
    assert all(p.configured for p in listing.producers)


@pytest.mark.asyncio
async def test_a_registered_producer_nobody_knows_reports_UNKNOWN_returns() -> None:
    """The registry is open by design — a new backend is a config entry. So the service will meet
    names it has no knowledge of, and it must say so rather than assume the common case."""
    rows = _by_name(producer_listing(_settings({"yolo-world": "http://y:1"})))

    assert rows["yolo-world"].configured
    assert rows["yolo-world"].returns == [], "the service invented a capability it cannot know"
    assert rows["yolo-world"].compatible is None


@pytest.mark.asyncio
async def test_compatibility_against_a_task_is_answered_BEFORE_anyone_runs_the_model(state_settings: AnnotatorSettings) -> None:
    """The other half of "schemas must align from the ml backend".

    A producer emitting polygons for a bbox-only task is answerable from config alone. Discovering
    it by running the model, reviewing the output and being refused at submit is the long way round.
    """
    store = await _store_with_task(state_settings, {"kind": "object-detection", "classes": [{"name": "region", "tools": ["bbox"]}]})
    try:
        rows = _by_name(producer_listing(_settings(), await enforced_shape_types(store, "p1", "t1")))
    finally:
        await store.close()

    assert rows["grounding-dino"].compatible is True, "a detector is compatible with a bbox task"
    assert rows["sam"].compatible is False, "a segmenter was reported fit for a bbox-only task"


@pytest.mark.asyncio
async def test_a_task_of_another_project_answers_as_an_unknown_task(state_settings: AnnotatorSettings) -> None:
    """The gate checked the project the request names (`?project=`), so a task is read in that project only: someone who
    may annotate p2 and names p1's t1 gets the answer an unknown id gets, which carries no part of t1's ontology and does
    not tell that t1 exists."""
    store = await _store_with_task(state_settings, {"kind": "object-detection", "classes": [{"name": "region", "tools": ["bbox"]}]})
    try:
        await store.create_project(AnnotationProject(project_id="p2", tenant="acme", slug="p2"))
        foreign = producer_listing(_settings(), await enforced_shape_types(store, "p2", "t1"))
        unknown = producer_listing(_settings(), await enforced_shape_types(store, "p2", "no-such-task"))
    finally:
        await store.close()

    assert foreign == unknown
    assert all(row.compatible is None for row in foreign.producers)


@pytest.mark.asyncio
async def test_a_task_that_CONSTRAINS_NOTHING_makes_no_claim_either(state_settings: AnnotatorSettings) -> None:
    """An ontology with no classes declares no rule. Marking a producer "incompatible" against one
    would warn about a submission the server would have accepted."""
    store = await _store_with_task(state_settings, {"kind": "object-detection", "classes": []})
    try:
        listing = producer_listing(_settings(), await enforced_shape_types(store, "p1", "t1"))
    finally:
        await store.close()

    assert all(row.compatible is None for row in listing.producers)


@pytest.mark.asyncio
async def test_an_unreachable_store_makes_no_claim_rather_than_a_wrong_one(state_settings: AnnotatorSettings) -> None:
    store = await AnnotatorStore.open(state_settings)
    await store.close()

    listing = producer_listing(_settings(), await enforced_shape_types(store, "p1", "t1"))

    assert all(p.compatible is None for p in listing.producers)
    assert len(listing.producers) >= 2, "a transport failure emptied the settings surface"


def test_the_returns_table_uses_the_SAME_longest_prefix_rule_as_the_registry() -> None:
    # `sam-hq-tiny` must inherit `sam`'s polygons, exactly as it inherits `sam`'s backend. Two
    # different matching rules over the same producer names would eventually disagree.
    assert returns_for("sam-hq-tiny") == ("polygon",)
    assert returns_for("grounding-dino-swinb") == ("bbox",)
    assert returns_for("something-else") == ()


async def _store_with_task(settings: AnnotatorSettings, ontology: dict[str, Any]) -> AnnotatorStore:
    """A store holding task t1, whose captured ontology is ``ontology``; the caller closes it."""
    store = await AnnotatorStore.open(settings)
    captured = LabelOntology.model_validate(ontology)
    await store.create_project(AnnotationProject(project_id="p1", tenant="acme", slug="p1", ontology=captured))
    task = Task(task_id="t1", project_id="p1", source=ItemSource(kind="chunks"), media=MediaRef(kind="image"), ontology=captured)
    await store.send_tasks("p1", [task])
    return store
