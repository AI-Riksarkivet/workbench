"""A judge flags the labels of a submitted item for its reviewer, and decides nothing (ADR 0026, ADR 0033).

The annotator's real doors over HTTP on a real PostgreSQL, asking the stand-in endpoint on a real socket: its judge
disagrees with every label of the task's last class (`stamp`), and its failing model fails every ask.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.judgement import Flag, Verdict
from annotator.model_endpoint import stand_in
from annotator.projects.models import ItemSource, MediaRef, Shape, Task, TaskState
from annotator.state.store import AnnotatorStore
from tests.model_item import DATASET, UNIT, WORK, Build
from workbench.exceptions import ConflictError


REVIEW_LIST = {"include": ["details", "judge"], "sort": "judge", "state": "in_review"}
DOUBT = stand_in.doubt("stamp", "seal")
#: What the stand-in judge says of a box of class `stamp` whose shape is `s1`.
FLAGS = [{"shape_id": "s1", "reason": DOUBT, "suggested_label": "seal", "score": stand_in.JUDGE_SCORE}]


def _store(client: TestClient) -> AnnotatorStore:
    return cast("FastAPI", client.app).state.store


def _submit(client: TestClient) -> None:
    """The local person's work on t1 is one box of class `stamp`, submitted to review."""
    assert client.portal is not None
    shape = Shape(shape_id="s1", shape_type="bbox", x=1, y=2, width=3, height=4, label="stamp")
    client.portal.call(lambda: _store(client).change_draft("t1", author="anon", change=lambda _draft, _now: ([shape], []), base_revision=0))
    assert client.post("/tasks/t1/events", json={"event": "submit"}).status_code == 200


def _judge(client: TestClient) -> httpx.Response:
    return client.post(f"/api/judge/{UNIT}", params=WORK)


def _in_review(client: TestClient, task_id: str, submitted_at: datetime, *, replica_of: str | None = None) -> Verdict:
    """Another item of p1 in review, submitted by `bo` at ``submitted_at``; answers a verdict that flags it."""
    assert client.portal is not None
    task = Task(
        task_id=task_id,
        project_id="p1",
        state=TaskState.IN_REVIEW,
        submitted_by="bo",
        submitted_at=submitted_at,
        replica_of=replica_of,
        source=ItemSource(kind="chunks", keys=[f"{task_id}/0/0"], where=DATASET),
        media=MediaRef(kind="image"),
    )
    client.portal.call(_store(client).send_tasks, "p1", [task])
    return Verdict(
        task_id=task_id, submitted_at=submitted_at, revision=0, model="m", version="v1", asked_by="bo", flags=[Flag(shape_id=None, reason="unsure", score=0.5)]
    )


def test_the_review_list_puts_a_flagged_item_first_with_what_the_judge_said_and_its_version(
    client_for: Build, endpoint: str, asks: list[dict[str, object]]
) -> None:
    client = client_for(endpoint)
    # The project's manager names its judge in its settings.
    assert client.patch("/projects/p1", json={"judge_model": stand_in.JUDGE}).status_code == 200
    _submit(client)
    # Two older submissions: one a judge found nothing in, one no judge has read.
    clear = _in_review(client, "t-clear", datetime(2026, 1, 1, tzinfo=UTC)).model_copy(update={"flags": []})
    _in_review(client, "t-unread", datetime(2026, 1, 2, tzinfo=UTC))
    assert client.portal is not None
    client.portal.call(_store(client).record_judgement, clear)

    verdict = _judge(client)
    listing = client.get("/projects/p1/tasks", params=REVIEW_LIST)

    assert verdict.status_code == 200, verdict.text
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [d["task_id"] for d in body["details"]] == ["t1", "t-unread", "t-clear"]
    flagged = body["details"][0]["judgement"]
    assert (flagged["model"], flagged["version"], flagged["revision"], flagged["asked_by"]) == (stand_in.JUDGE, stand_in.VERSION, 1, "anon")
    assert flagged["flags"] == FLAGS
    assert body["details"][1]["judgement"] is None
    assert body["judge"] == {"model": stand_in.JUDGE, "available": True, "reason": "", "flagged": 1, "clear": 1, "unread": 1}
    # The judge was sent the submission's labels under their shape ids, and the task's classes.
    (ask,) = [seen["body"] for seen in asks if seen["path"] == "/v1/assessments"]
    assert isinstance(ask, dict)
    assert (ask["model"], ask["classes"], ask["requested_by"]) == (stand_in.JUDGE, ["seal", "stamp"], "anon")
    assert [(label["id"], label["shape_type"], label["label"]) for label in ask["labels"]] == [("s1", "bbox", "stamp")]


def test_a_judges_flag_moves_no_item(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint, judge=stand_in.JUDGE)
    _submit(client)
    before = client.get("/tasks/t1").json()

    verdict = _judge(client)

    assert (verdict.status_code, verdict.json()["flags"]) == (200, FLAGS)
    assert client.get("/tasks/t1").json() == before
    assert before["state"] == "in_review"


def test_a_submission_already_judged_answers_its_verdict_and_asks_no_model(client_for: Build, endpoint: str, asks: list[dict[str, object]]) -> None:
    client = client_for(endpoint, judge=stand_in.JUDGE)
    _submit(client)
    first = _judge(client)
    asks.clear()

    again = _judge(client)

    assert (again.status_code, again.json()) == (200, first.json())
    assert asks == []


def test_a_replicas_verdict_is_read_only_by_who_may_read_its_work(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint, judge=stand_in.JUDGE)
    assert client.portal is not None
    client.portal.call(_store(client).record_judgement, _in_review(client, "g1-r0", datetime(2026, 1, 1, tzinfo=UTC), replica_of="g1"))
    app = cast("FastAPI", client.app)

    async def views_only(*, user: str, relation: str, obj: str, contextual: object = None) -> bool:
        return relation == "can_view"

    # A fellow annotator of the item, who views the project and may not review it.
    app.dependency_overrides[current_subject] = lambda: "cy"
    app.dependency_overrides[get_checker] = lambda: views_only

    as_a_fellow_annotator = client.get("/projects/p1/tasks", params=REVIEW_LIST)

    app.dependency_overrides.pop(get_checker)
    as_a_reviewer = client.get("/projects/p1/tasks", params=REVIEW_LIST)
    assert [(d["task_id"], d["judgement"]) for d in as_a_fellow_annotator.json()["details"]] == [("g1-r0", None)]
    assert [d["judgement"]["flags"][0]["reason"] for d in as_a_reviewer.json()["details"]] == ["unsure"]


def test_a_verdict_that_arrives_once_the_item_is_decided_is_not_kept(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint, judge=stand_in.JUDGE)
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    late = _in_review(client, "t2", submitted_at)
    cast("FastAPI", client.app).dependency_overrides[current_subject] = lambda: "rita"
    assert client.post("/tasks/t2/events", json={"event": "accept"}).status_code == 200
    assert client.portal is not None

    with pytest.raises(ConflictError, match="left review"):
        client.portal.call(_store(client).record_judgement, late)

    assert client.portal.call(_store(client).judgement_of, "t2", submitted_at) is None


@pytest.mark.parametrize(
    ("judge", "status", "problem", "detail", "reason"),
    [
        pytest.param("", 409, "about:blank#no-judge", "this project names no judge", "this project names no judge", id="no-judge"),
        pytest.param(
            stand_in.FAILING,
            502,
            "about:blank#model-endpoint-failed",
            f"the model endpoint failed (500): {stand_in.FAILING} fails every ask",
            "",
            id="failing-judge",
        ),
    ],
)
def test_with_no_judge_or_a_failing_one_the_review_list_reads_as_before_and_says_so(
    client_for: Build, endpoint: str, judge: str, status: int, problem: str, detail: str, reason: str
) -> None:
    client = client_for(endpoint, judge=judge)
    _submit(client)

    asked = _judge(client)
    listing = client.get("/projects/p1/tasks", params=REVIEW_LIST)

    assert (asked.status_code, asked.json()["type"], asked.json()["detail"]) == (status, problem, detail)
    assert asked.headers["content-type"] == "application/problem+json"
    body = listing.json()
    assert listing.status_code == 200, listing.text
    assert [(d["task_id"], d["state"], d["judgement"]) for d in body["details"]] == [("t1", "in_review", None)]
    assert body["judge"] == {"model": judge, "available": not reason, "reason": reason, "flagged": 0, "clear": 0, "unread": 1}


@pytest.mark.parametrize(
    ("decision", "action", "agreed"),
    [
        pytest.param({"event": "accept"}, "accepted", False, id="accepting-as-it-is-disagrees"),
        pytest.param({"event": "request_changes", "message": "not a stamp"}, "request_changes", True, id="sending-back-agrees"),
        pytest.param({"event": "accept", "judge_agreed": True}, "accepted", True, id="what-the-reviewer-says-decides"),
    ],
)
def test_a_reviewers_decision_on_a_flagged_item_records_whether_they_agreed(
    client_for: Build, endpoint: str, decision: dict[str, object], action: str, agreed: bool
) -> None:
    client = client_for(endpoint, judge=stand_in.JUDGE)
    _submit(client)
    submitted_at = datetime.fromisoformat(client.get("/tasks/t1").json()["submitted_at"])
    assert _judge(client).status_code == 200
    cast("FastAPI", client.app).dependency_overrides[current_subject] = lambda: "rita"

    decided = client.post("/tasks/t1/events", json=decision)

    assert decided.status_code == 200, decided.text
    assert client.portal is not None
    kept = client.portal.call(_store(client).judgement_of, "t1", submitted_at)
    assert kept is not None
    assert (kept.agreed, kept.settled_by, kept.settled_action) == (agreed, "rita", action)
    assert kept.settled_at == datetime.fromisoformat(decided.json()["reviewed_at"])


def test_a_suggested_class_the_flagged_labels_shape_cannot_take_is_dropped_and_the_flag_kept(client_for: Build, endpoint: str) -> None:
    """The stand-in judge suggests `seal` for a box of class `stamp`; here `seal` takes only whole-item tags, so a reviewer
    could never choose it for that box. The flag, its reason and its score stand; the suggestion goes."""
    client = client_for(endpoint, judge=stand_in.JUDGE, tools_of={"seal": ["tag"]})
    _submit(client)

    verdict = _judge(client)

    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["flags"] == [{**FLAGS[0], "suggested_label": ""}]
