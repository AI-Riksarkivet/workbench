"""A project's sampling strategy orders its queue and proposes what to send, and moves nothing (ADR 0027, ADR 0034,
ADR 0035).

The annotator's real doors over HTTP on a real PostgreSQL, reading a real Lance dataset and asking the stand-in endpoint
on a real socket. Its scorer proposes one tag of the task's first class (`seal`) on each item, scored by a fixed function
of the item's id (`stand_in.score_of`), so the doubt of each item, 1 less that score, is known here; the dataset's
embeddings are two numbers a row (`VECTORS`), so every cosine distance is worked out by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.model_endpoint import stand_in
from annotator.projects.models import Shape
from annotator.sampling import NO_SAMPLING_ENDPOINT, NO_SAMPLING_MODEL, NOTHING_MEASURED, SAMPLING_OFF
from annotator.state.store import AnnotatorStore
from tests.model_item import DATASET, DOC, EMBEDDER, Build


#: The documents of tasks t2 to t5, beside t1's (`DOC`).
MORE_DOCS = ("b" * 16, "c" * 16, "d" * 16, "e" * 16)
DOC_OF = {"t1": DOC} | {f"t{n}": doc for n, doc in enumerate(MORE_DOCS, start=2)}
#: Classes whose first, `seal`, takes the scorer's tag.
TAGGED: dict[str, Sequence[str]] = {"seal": ["tag"]}
LEAST_SURE = {"sampling": "least_sure", "sampling_model": stand_in.SCORER}
SAMPLED = {"include": ["details", "sampling"], "sort": "sampling"}
#: Each item's embedding. From t1's (1, 0) the cosine distances are t2 0.005, t5 0.293, t3 1 and t4 2.
VECTORS = {DOC: [1.0, 0.0], MORE_DOCS[0]: [1.0, 0.1], MORE_DOCS[1]: [0.0, 1.0], MORE_DOCS[2]: [-1.0, 0.0], MORE_DOCS[3]: [0.7, 0.7]}
FROM_T1 = {"t2": 1 - 1 / 1.01**0.5, "t3": 1.0, "t4": 2.0, "t5": 1 - 0.5**0.5}
#: t4's (-1, 0) from t2's (1, 0.1).
T4_FROM_T2 = 1 + 1 / 1.01**0.5
#: The score column: the scorer was 0.1 sure of t2's item and 0.5 of t3's, and a pipeline scored no other.
CONFIDENCES = {MORE_DOCS[0]: 0.1, MORE_DOCS[1]: 0.5}


def _doubt(task_id: str) -> float:
    """The scorer's doubt on a task's item: 1 less its score on the item's id, as the annotator names the item."""
    return 1 - stand_in.score_of(f"{DATASET}/{DOC_OF[task_id]}/0/19")


def _sample(client: TestClient, task_id: str) -> httpx.Response:
    return client.post(f"/api/sampling/{DOC_OF[task_id]}/0/19", params={"project": "p1", "task": task_id, "dataset": DATASET})


def _read(client: TestClient, *task_ids: str) -> httpx.Response:
    return client.post("/projects/p1/sampling/reads", json={"task_ids": list(task_ids)})


def _unit(task_id: str) -> str:
    return f"{DOC_OF[task_id]}/0/19"


def _label_t1(client: TestClient) -> None:
    """t1, which the local person holds, is tagged `seal` and submitted: the project's one labelled item."""
    assert client.portal is not None
    store: AnnotatorStore = cast("FastAPI", client.app).state.store
    tag = Shape(shape_id="s1", shape_type="tag", label="seal")
    client.portal.call(lambda: store.change_draft("t1", author="anon", change=lambda _draft, _now: ([tag], []), base_revision=0))
    assert client.post("/tasks/t1/events", json={"event": "submit"}).status_code == 200


def _listed(client: TestClient, **params: Any) -> list[str]:
    return [d["task_id"] for d in client.get("/projects/p1/tasks", params=SAMPLED | params).json()["details"]]


def _sampled(client_for: Build, endpoint: str | None, settings: dict[str, str]) -> TestClient:
    client = client_for(endpoint, tools_of=TAGGED, more_docs=MORE_DOCS)
    assert client.patch("/projects/p1", json=settings).status_code == 200
    return client


def test_least_sure_lists_the_items_by_descending_doubt_then_those_with_none_then_the_unscored(client_for: Build, endpoint: str) -> None:
    client = _sampled(client_for, endpoint, LEAST_SURE)
    for task_id in ("t1", "t2", "t3"):
        assert _sample(client, task_id).status_code == 200
    # t4's classes take neither a tag nor a box now, so the scorer proposes nothing it takes; t5 is never scored.
    no_tag = {"ontology": {"classes": [{"name": "seal", "tools": ["polygon"]}, {"name": "stamp", "tools": ["polygon"]}]}}
    assert client.patch("/projects/p1/ontology", json=no_tag).status_code == 200
    assert _sample(client, "t4").status_code == 200

    sampled = client.get("/projects/p1/tasks", params=SAMPLED)
    unscored = _listed(client, scored="false")
    assert client.patch("/projects/p1", json={"sampling": "off"}).status_code == 200
    plain = _listed(client)

    assert sampled.status_code == 200, sampled.text
    body = sampled.json()
    by_doubt = sorted(("t1", "t2", "t3"), key=_doubt, reverse=True)
    assert [d["task_id"] for d in body["details"]] == [*by_doubt, "t4", "t5"]
    assert [d["sampling"] and d["sampling"]["score"]["kind"] for d in body["details"]] == ["least_sure"] * 3 + ["proposed_nothing", None]
    assert [d["sampling"] and d["sampling"]["placed_by"] for d in body["details"]] == ["least_sure"] * 3 + [None, None]
    assert body["sampling"] == {
        "strategy": "least_sure",
        "model": stand_in.SCORER,
        "available": True,
        "reason": "",
        "asks": True,
        "scored": 4,
        "unscored": 1,
        "measured": 0,
        "unmeasured": 5,
    }
    assert unscored == ["t5"]
    assert plain == ["t1", "t2", "t3", "t4", "t5"]


def test_a_score_names_its_kind_its_doubt_the_model_its_version_and_who_asked(client_for: Build, endpoint: str, asks: list[dict[str, object]]) -> None:
    client = _sampled(client_for, endpoint, LEAST_SURE)

    scored = _sample(client, "t2")

    assert scored.status_code == 200, scored.text
    score = scored.json()
    assert {k: v for k, v in score.items() if k != "scored_at"} == {
        "task_id": "t2",
        "kind": "least_sure",
        "score": _doubt("t2"),
        "model": stand_in.SCORER,
        "version": stand_in.VERSION,
        "asked_by": "anon",
    }
    listed = client.get("/projects/p1/tasks", params=SAMPLED | {"limit": 1}).json()["details"]
    assert listed == [{**listed[0], "sampling": {"score": score, "novelty": None, "placed_by": "least_sure"}}]
    # The model was asked as the person, with the task's classes and no prompt.
    (ask,) = [seen["body"] for seen in asks if seen["path"] == "/v1/proposals"]
    assert isinstance(ask, dict)
    assert (ask["model"], ask["classes"], ask["requested_by"], ask["prompts"]) == (
        stand_in.SCORER,
        ["seal", "stamp"],
        "anon",
        {"text": None, "box": None, "points": []},
    )


def test_an_item_already_scored_answers_its_score_and_asks_no_model(client_for: Build, endpoint: str, asks: list[dict[str, object]]) -> None:
    client = _sampled(client_for, endpoint, LEAST_SURE)
    first = _sample(client, "t1")
    asks.clear()

    again = _sample(client, "t1")

    assert (again.status_code, again.json()) == (200, first.json())
    assert asks == []


def test_another_sampling_model_makes_every_item_unscored_until_it_scores_it(client_for: Build, endpoint: str) -> None:
    client = _sampled(client_for, endpoint, LEAST_SURE)
    assert _sample(client, "t2").status_code == 200

    assert client.patch("/projects/p1", json={"sampling_model": stand_in.TAGGER}).status_code == 200
    unscored = client.get("/projects/p1/tasks", params=SAMPLED).json()
    rescored = _sample(client, "t2")

    assert [d["sampling"] for d in unscored["details"]] == [None] * 5
    assert (unscored["sampling"]["scored"], unscored["sampling"]["unscored"]) == (0, 5)
    assert (rescored.status_code, rescored.json()["model"], rescored.json()["score"]) == (200, stand_in.TAGGER, 1 - stand_in.TAGGER_SCORE)


@pytest.mark.parametrize(
    ("served", "settings", "status", "problem", "reason", "listed"),
    [
        pytest.param(False, LEAST_SURE, 503, "about:blank#no-model-endpoint", NO_SAMPLING_ENDPOINT, NO_SAMPLING_ENDPOINT, id="no-endpoint"),
        # No endpoint and no embeddings: neither half of a mix has anything to order by.
        pytest.param(
            False,
            {"sampling": "mix", "sampling_model": stand_in.SCORER},
            503,
            "about:blank#no-model-endpoint",
            NO_SAMPLING_ENDPOINT,
            f"{NO_SAMPLING_ENDPOINT}, and {NOTHING_MEASURED}",
            id="no-endpoint-and-nothing-measured",
        ),
        pytest.param(True, {"sampling": "least_sure"}, 409, "about:blank#no-sampling", NO_SAMPLING_MODEL, NO_SAMPLING_MODEL, id="no-model"),
        pytest.param(
            True, {"sampling": "off", "sampling_model": stand_in.SCORER}, 409, "about:blank#no-sampling", SAMPLING_OFF, SAMPLING_OFF, id="sampling-off"
        ),
    ],
)
def test_a_project_that_cannot_sample_says_so_and_its_list_reads_in_item_order(
    client_for: Build, endpoint: str, served: bool, settings: dict[str, str], status: int, problem: str, reason: str, listed: str
) -> None:
    client = _sampled(client_for, endpoint if served else None, settings)

    asked = _sample(client, "t3")
    listing = client.get("/projects/p1/tasks", params=SAMPLED)

    assert (asked.status_code, asked.json()["type"], asked.json()["detail"]) == (status, problem, reason)
    assert asked.headers["content-type"] == "application/problem+json"
    body = listing.json()
    strategy = settings["sampling"]
    assert body["sampling"] == {
        "strategy": strategy,
        "model": settings.get("sampling_model", ""),
        "available": False,
        "reason": listed,
        "asks": False,
        "scored": 0,
        "unscored": 5,
        "measured": 0,
        "unmeasured": 5,
    }
    expected = ["t1", "t2", "t3", "t4", "t5"]
    assert [d["task_id"] for d in body["details"]] == expected


def test_a_score_moves_no_item_and_leaves_its_draft(client_for: Build, endpoint: str) -> None:
    client = _sampled(client_for, endpoint, LEAST_SURE)
    task, draft = client.get("/tasks/t1").json(), client.get("/tasks/t1/draft").json()

    scored = _sample(client, "t1")

    assert scored.status_code == 200, scored.text
    assert (client.get("/tasks/t1").json(), client.get("/tasks/t1/draft").json()) == (task, draft)
    assert (task["state"], task["assignee"]) == ("claimed", "anon")


def test_least_alike_lists_the_items_by_their_distance_from_the_labelled_ones(client_for: Build) -> None:
    # The labelled t1 names no dataset: it is one of the deployment's default dataset, the one the others name.
    client = client_for(None, tools_of=TAGGED, more_docs=MORE_DOCS, vectors=VECTORS, default_dataset=True)
    assert client.patch("/projects/p1", json={"sampling": "least_alike"}).status_code == 200
    _label_t1(client)

    read = _read(client, "t2", "t3", "t4", "t5")

    assert read.status_code == 200, read.text
    assert read.json()["notes"] == []
    measured = {m["task_id"]: m for m in read.json()["measured"]}
    assert {task_id: m["novelty"] for task_id, m in measured.items()} == pytest.approx(FROM_T1, abs=1e-6)
    assert {k: v for k, v in measured["t4"].items() if k not in ("novelty", "measured_at")} == {
        "task_id": "t4",
        "against": 1,
        "vectors": "chunks.embedding",
        "model": EMBEDDER,
        "asked_by": "anon",
    }
    body = client.get("/projects/p1/tasks", params=SAMPLED).json()
    assert [(d["task_id"], d["sampling"] and d["sampling"]["placed_by"]) for d in body["details"]] == [
        ("t4", "least_alike"),
        ("t3", "least_alike"),
        ("t5", "least_alike"),
        ("t2", "least_alike"),
        ("t1", None),
    ]
    assert (body["sampling"]["available"], body["sampling"]["measured"], body["sampling"]["unmeasured"]) == (True, 4, 1)


@pytest.mark.parametrize(
    ("vectors", "note"),
    [
        pytest.param(None, f"{DATASET} declares no embeddings, so diversity is unavailable for its items", id="no-embeddings"),
        pytest.param(VECTORS, f"no item of {DATASET} is labelled yet, so there is nothing to measure its items against", id="nothing-labelled"),
    ],
)
def test_diversity_that_cannot_be_measured_says_why_and_the_list_reads_in_item_order(
    client_for: Build, vectors: dict[str, list[float]] | None, note: str
) -> None:
    client = client_for(None, tools_of=TAGGED, more_docs=MORE_DOCS, vectors=vectors)
    assert client.patch("/projects/p1", json={"sampling": "least_alike"}).status_code == 200

    read = _read(client, "t2", "t3")

    assert (read.status_code, read.json()) == (200, {"scored": [], "measured": [], "ask": [], "notes": [note]})
    assert _listed(client) == ["t1", "t2", "t3", "t4", "t5"]


def test_mix_takes_the_score_column_and_interleaves_doubt_with_distance(client_for: Build, endpoint: str, asks: list[dict[str, object]]) -> None:
    client = client_for(endpoint, tools_of=TAGGED, more_docs=MORE_DOCS, vectors=VECTORS, confidences=CONFIDENCES, scored_by=stand_in.SCORER)
    assert client.patch("/projects/p1", json={"sampling": "mix", "sampling_model": stand_in.SCORER}).status_code == 200
    _label_t1(client)

    read = _read(client, "t2", "t3", "t4", "t5")

    assert read.status_code == 200, read.text
    answer = read.json()
    # The column's scores are kept as the scorer's doubt, and no model is asked for them; the two the column leaves out
    # are the app's to ask the endpoint about.
    assert [(s["task_id"], s["kind"], s["score"], s["model"]) for s in answer["scored"]] == [
        ("t2", "least_sure", pytest.approx(0.9), stand_in.SCORER),
        ("t3", "least_sure", pytest.approx(0.5), stand_in.SCORER),
    ]
    assert (answer["ask"], answer["notes"], asks) == (["t4", "t5"], [], [])
    # Doubt's first, distance's first, doubt's second, then the next by distance not yet placed.
    listed = client.get("/projects/p1/tasks", params=SAMPLED).json()["details"]
    assert [(d["task_id"], d["sampling"] and d["sampling"]["placed_by"]) for d in listed] == [
        ("t2", "least_sure"),
        ("t4", "least_alike"),
        ("t3", "least_sure"),
        ("t5", "least_alike"),
        ("t1", None),
    ]


def _propose(client: TestClient, count: int) -> httpx.Response:
    keys = [_unit(task_id) for task_id in ("t2", "t3", "t4", "t5")]
    return client.post("/projects/p1/sampling/proposals", json={"dataset": DATASET, "keys": keys, "count": count})


def _item(task_id: str, unit: str) -> dict[str, object]:
    return {"task_id": task_id, "source": {"kind": "chunks", "keys": [unit], "where": DATASET}, "media": {"kind": "image"}}


def test_a_send_names_a_proposal_and_each_item_records_whether_sampling_or_the_person_chose_it(client_for: Build) -> None:
    client = client_for(None, tools_of=TAGGED, more_docs=MORE_DOCS, vectors=VECTORS, confidences=CONFIDENCES, scored_by=stand_in.SCORER)
    assert client.patch("/projects/p1", json={"sampling": "mix", "sampling_model": stand_in.SCORER}).status_code == 200
    _label_t1(client)

    proposed = _propose(client, 3)
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()
    items = [_item("s2", _unit("t2")), _item("s4", _unit("t4")), _item("s5", _unit("t5"))]
    sent = client.post("/projects/p1/items", json={"items": items, "proposal": proposal["proposal_id"]})

    # Doubt's first (t2's item), then the farthest from the labelled t1 and the chosen t2 (t4's, nearer t2 than t1), then
    # doubt's second (t3's).
    assert (proposal["strategy"], proposal["notes"]) == ("mix", [])
    assert proposal["chosen"] == [
        {"key": _unit("t2"), "kind": "least_sure", "score": pytest.approx(0.9), "model": stand_in.SCORER},
        {"key": _unit("t4"), "kind": "least_alike", "score": pytest.approx(T4_FROM_T2), "model": EMBEDDER},
        {"key": _unit("t3"), "kind": "least_sure", "score": pytest.approx(0.5), "model": stand_in.SCORER},
    ]
    assert sent.status_code == 201, sent.text
    assert [client.get(f"/tasks/{task_id}").json()["chosen"] for task_id in ("s2", "s4", "s5")] == [
        {"by": "sampling", "kind": "least_sure", "score": pytest.approx(0.9), "model": stand_in.SCORER},
        {"by": "sampling", "kind": "least_alike", "score": pytest.approx(T4_FROM_T2), "model": EMBEDDER},
        {"by": "person", "kind": None, "score": None, "model": None},
    ]


def test_least_sure_with_no_score_column_proposes_by_distance_and_says_so(client_for: Build, endpoint: str, asks: list[dict[str, object]]) -> None:
    # The dataset declares its embeddings as a vector binding of its search, and nothing for sampling.
    client = client_for(endpoint, tools_of=TAGGED, more_docs=MORE_DOCS, vectors=VECTORS, searchable=True)
    assert client.patch("/projects/p1", json=LEAST_SURE).status_code == 200
    _label_t1(client)

    proposed = _propose(client, 2)

    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["notes"] == [
        f"{DATASET} carries no score by {stand_in.SCORER}: least sure orders these items once they are in the project, "
        "and they are proposed by how unlike the labelled items they are"
    ]
    assert [(c["key"], c["kind"], c["score"]) for c in proposed.json()["chosen"]] == [
        (_unit("t4"), "least_alike", pytest.approx(2.0)),
        (_unit("t3"), "least_alike", pytest.approx(1.0)),
    ]
    assert asks == []


def test_a_send_naming_a_proposal_the_project_does_not_hold_is_refused_whole(client_for: Build) -> None:
    client = client_for(None, tools_of=TAGGED, more_docs=MORE_DOCS)

    sent = client.post("/projects/p1/items", json={"items": [_item("s2", _unit("t2"))], "proposal": "no-such-proposal"})

    assert (sent.status_code, sent.json()["type"]) == (409, "about:blank#no-such-proposal")
    assert client.get("/tasks/s2").status_code == 404
