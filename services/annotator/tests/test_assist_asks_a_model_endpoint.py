"""Assist asks the deployment's model endpoint by the workbench's model protocol, and a proposal is a person's to settle.

The annotator's real doors over HTTP on a real PostgreSQL, calling the stand-in endpoint on a real socket
(`annotator.model_endpoint.stand_in` under uvicorn), which is every way an endpoint can behave: it answers, it never
answers, it is busy, it fails. The same ways are asked of a Ray Serve deployment that speaks the protocol
(`tests.ray_model`, on a Ray the module starts), the backend a cluster runs (ADR 0032). With no endpoint named, assist
says it is unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pyarrow as pa
import pytest
import ray
import respx
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance import blob_array, blob_field
from pydantic import BaseModel, ConfigDict
from ray import serve
from starlette.types import Message, Receive, Scope, Send

from annotator.annotations.router import router as annotations_router
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import assist, tasks
from annotator.core.config import AnnotatorSettings
from annotator.model_endpoint import protocol, stand_in
from annotator.model_endpoint.client import MAX_ANSWER_BYTES
from annotator.projects.models import AnnotationProject, ItemSource, MediaRef, Shape, Task, TaskState
from annotator.projects.ontology import LabelClass, LabelOntology
from client_kit.media.state import AppState
from tests import ray_model
from tests.identity import Provider
from tests.media_catalog import write_table


SOURCE = "scans"
DATASET = f"{SOURCE}~acme$proj1"
DOC = "a" * 16
UNIT = f"{DOC}/0/19"
QUERY = {"project": "p1", "dataset": DATASET}
WORK = {"project": "p1", "task": "t1", "dataset": DATASET}
UNAVAILABLE = "assist is unavailable: this deployment names no model endpoint"
#: The unit's frame: what a model is sent, byte for byte.
PICTURE = b"\x89PNG\r\n\x1a\n-a-picture"


#: Every call the running stand-in was sent, by the test that is running.
_ASKS: list[dict[str, object]] = []


@pytest.fixture(scope="module")
def running_stand_in() -> Iterator[str]:
    """The stand-in model endpoint on a socket of its own, recording what it is asked; its address."""
    app = stand_in.create_stand_in()

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        """The stand-in, with each request it reads kept; its answer passes through as it is sent, byte by byte."""
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        body = bytearray()

        async def read() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    headers = dict(scope["headers"])
                    _ASKS.append(
                        {"path": scope["path"], "authorization": headers.get(b"authorization", b"").decode(), "body": json.loads(body) if body else None}
                    )
            return message

        await app(scope, read, send)

    server = uvicorn.Server(uvicorn.Config(recording, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the stand-in endpoint did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def endpoint(running_stand_in: str) -> str:
    """The stand-in's address, with nothing asked of it yet."""
    _ASKS.clear()
    return running_stand_in


#: The tests that ask Ray Serve share one Ray, on one test worker (`--dist loadgroup` in the lanes): each Ray takes about
#: 6 s to start and 260 MB, measured on this host.
ON_RAY = pytest.mark.xdist_group("ray-serve")
#: The audience the annotator's projected ServiceAccount token names (the chart's `models.endpoint.audience`), and the
#: subject a cluster's issuer gives the annotator's ServiceAccount.
MODEL_AUDIENCE = "workbench-models"
ANNOTATOR = "system:serviceaccount:workbench:workbench-annotator"
ROOT = Path(__file__).parents[3]


class RayServe(BaseModel):
    """The running Ray Serve deployment: its address (a route prefix behind Serve's proxy) and the issuer it trusts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    issuer: Provider

    def token(self, subject: str = ANNOTATOR, *, audience: str = MODEL_AUDIENCE, lifetime: int = 300, omit: Sequence[str] = (), **claims: str) -> str:
        """A token the deployment's issuer signs, for the annotator unless ``subject`` says otherwise."""
        return self.issuer.mint(subject, audience=audience, lifetime=lifetime, omit=omit, **claims)


@pytest.fixture(scope="module")
def ray_serve() -> Iterator[RayServe]:
    """A Ray of the module's own, with Serve's HTTP proxy on a free port and `tests.ray_model` deployed at `/models`."""
    issuer = Provider()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        # A Ray of its own (`local`), never one `RAY_ADDRESS` or a running `ray start` names.
        ray.init(
            address="local",
            num_cpus=2,
            include_dashboard=False,
            log_to_driver=False,
            configure_logging=False,
            runtime_env={"env_vars": {"PYTHONPATH": str(ROOT)}},
        )
        serve.start(http_options={"host": "127.0.0.1", "port": port})
        serve.run(ray_model.application(f"{issuer.issuer}/jwks", issuer.issuer, MODEL_AUDIENCE, ANNOTATOR), name="models", route_prefix="/models")
        yield RayServe(url=f"http://127.0.0.1:{port}/models", issuer=issuer)
    finally:
        if ray.is_initialized():
            serve.shutdown()
            ray.shutdown()
        issuer.close()


def _serving(request: pytest.FixtureRequest, serves: str) -> tuple[str, str]:
    """The address of the endpoint a test asks, and the token the annotator presents to it."""
    if serves == "ray":
        running = request.getfixturevalue("ray_serve")
        return running.url, running.token()
    return request.getfixturevalue("endpoint"), "the-annotators-own-token"


Build = Callable[..., TestClient]


@pytest.fixture
def client_for(state_app: Callable[..., FastAPI], tmp_path: Path) -> Iterator[Build]:
    """The assist, annotations and task doors over the store, on a dataset whose unit has a frame; project p1 holds task
    t1 (the unit, claimed by the local person, labelling with the classes `seal` and `stamp`). ``endpoint`` names the
    model endpoint, or none; ``presents`` is the token the annotator presents to it, ``picture`` the unit's frame."""
    key_fields = ["doc_id", "speech_id", "chunk_id"]
    descriptor = {"identity": {"key_fields": key_fields}, "capabilities": {"frames": "frames.frame"}}
    keys = {"doc_id": [DOC], "speech_id": pa.array([0], pa.int64()), "chunk_id": pa.array([19], pa.int64())}
    write_table(tmp_path, ["acme", "proj1", "chunks"], pa.table(keys), descriptor=descriptor)
    schema = pa.schema(
        [
            ("doc_id", pa.string()),
            ("speech_id", pa.int64()),
            ("chunk_id", pa.int64()),
            ("frame_idx", pa.int64()),
            ("frame_mime", pa.string()),
            blob_field("frame"),
        ]
    )
    token = tmp_path / "model-endpoint-token"
    opened: list[TestClient] = []

    def build(
        endpoint: str | None,
        *,
        timeout: float = 30.0,
        tools: Sequence[str] = ("bbox", "polygon"),
        presents: str = "the-annotators-own-token",
        picture: bytes = PICTURE,
    ) -> TestClient:
        frames = pa.table(
            {**keys, "frame_idx": pa.array([0], pa.int64()), "frame_mime": ["image/png; charset=binary"], "frame": blob_array([picture])},
            schema=schema,
        )
        write_table(tmp_path, ["acme", "proj1", "frames"], frames)
        token.write_text(presents + "\n")
        values: dict[str, object] = {
            "WORKBENCH_SOURCES": [{"kind": "namespace", "name": SOURCE, "impl": "dir", "properties": {"root": str(tmp_path)}, "annotatable": True}],
            "ANNOTATOR_MODEL_ENDPOINT_TIMEOUT_SECONDS": timeout,
        }
        if endpoint is not None:
            values |= {"ANNOTATOR_MODEL_ENDPOINT_URL": endpoint, "ANNOTATOR_MODEL_ENDPOINT_TOKEN_FILE": str(token)}
        app = state_app(assist.router, annotations_router, tasks.router)
        app.state.resources = AppState(settings=AnnotatorSettings.model_validate(values))
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        store, portal = app.state.store, client.portal
        assert portal is not None, "the client is not running the app"
        ontology = LabelOntology(classes=[LabelClass.model_validate({"name": name, "tools": list(tools)}) for name in ("seal", "stamp")])
        portal.call(store.create_project, AnnotationProject(project_id="p1", tenant="acme", slug="p1", ontology=ontology))
        task = Task(
            task_id="t1",
            project_id="p1",
            state=TaskState.CLAIMED,
            assignee="anon",
            source=ItemSource(kind="chunks", keys=[UNIT], where=DATASET),
            media=MediaRef(kind="image"),
            ontology=ontology,
        )
        portal.call(store.send_tasks, "p1", [task])
        return client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def _ask(client: TestClient, model: str, **prompts: object) -> httpx.Response:
    return client.post(f"/api/assist/{UNIT}", params=QUERY, json={"model": model, "task_id": "t1", **prompts})


def test_assist_answers_the_endpoints_proposals_with_their_model_version_and_score(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)

    answer = _ask(client, stand_in.DETECTOR, prompt="a seal", region={"x": 10, "y": 20, "width": 30, "height": 40})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "model": stand_in.DETECTOR,
        "version": stand_in.VERSION,
        "proposals": [
            {
                "shape_type": "bbox",
                "x": 10.0,
                "y": 20.0,
                "width": 30.0,
                "height": 40.0,
                "polygon": [],
                "label": "seal",
                "text": "",
                "score": stand_in.DETECTOR_SCORE,
            }
        ],
        "dropped": [],
    }
    # The ask is the protocol's: the annotator's own token, the unit's picture carried in it, the task's classes, the
    # person it was made for.
    (ask,) = [seen for seen in _ASKS if seen["path"] == "/v1/proposals"]
    assert ask["authorization"] == "Bearer the-annotators-own-token"
    assert ask["body"] == {
        "model": stand_in.DETECTOR,
        "item": {"id": f"{DATASET}/{UNIT}", "content": {"kind": "image", "mime": "image/png", "data": base64.b64encode(PICTURE).decode()}},
        "prompts": {"text": "a seal", "box": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}, "points": []},
        "classes": ["seal", "stamp"],
        "requested_by": "anon",
    }


def test_the_models_a_task_can_ask_are_the_endpoints_with_what_each_can_do(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)

    listing = client.get("/api/assist/models", params={"project": "p1", "task_id": "t1"})

    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert (body["available"], body["reason"]) == (True, "")
    assert body["models"][:2] == [
        {
            "name": stand_in.DETECTOR,
            "version": stand_in.VERSION,
            "title": "Stand-in detector",
            "reads": ["image"],
            "prompts": ["text", "box"],
            "returns": ["bbox"],
            "compatible": True,
        },
        {
            "name": stand_in.SEGMENTER,
            "version": stand_in.VERSION,
            "title": "Stand-in segmenter",
            "reads": ["image"],
            "prompts": ["box", "points"],
            "returns": ["polygon"],
            "compatible": True,
        },
    ]


@pytest.mark.parametrize(
    ("serves", "model", "timeout", "status", "detail", "retry_after"),
    [
        pytest.param("stand-in", stand_in.SLOW, 0.2, 504, "the model endpoint did not answer within 0.2 s", None, id="stand-in-slow"),
        pytest.param(
            "stand-in", stand_in.FAILING, 30.0, 502, f"the model endpoint failed (500): {stand_in.FAILING} fails every ask", None, id="stand-in-failing"
        ),
        pytest.param("ray", ray_model.SLOW, 0.5, 504, "the model endpoint did not answer within 0.5 s", None, id="ray-slow", marks=ON_RAY),
        pytest.param(
            "ray", ray_model.BUSY, 30.0, 503, f"the model endpoint is busy or unavailable for now: {ray_model.BUSY} is busy", "7", id="ray-busy", marks=ON_RAY
        ),
        pytest.param(
            "ray", ray_model.FAILING, 30.0, 502, f"the model endpoint failed (500): {ray_model.FAILING} fails every ask", None, id="ray-failing", marks=ON_RAY
        ),
    ],
)
def test_an_endpoint_that_is_slow_busy_or_failing_is_an_error_the_person_reads(
    request: pytest.FixtureRequest, client_for: Build, serves: str, model: str, timeout: float, status: int, detail: str, retry_after: str | None
) -> None:
    url, presents = _serving(request, serves)
    client = client_for(url, timeout=timeout, presents=presents)

    answer = _ask(client, model, prompt="a seal")

    assert (answer.status_code, answer.headers["content-type"], answer.headers.get("retry-after")) == (status, "application/problem+json", retry_after), (
        answer.text
    )
    assert answer.json()["detail"] == detail


@ON_RAY
def test_assist_answers_from_a_ray_serve_deployment_that_was_shown_the_whole_full_size_picture(client_for: Build, ray_serve: RayServe) -> None:
    # The largest picture the protocol carries: 16 MiB, 22 MB once base64, through Serve's HTTP proxy.
    picture = bytes(range(256)) * (protocol.MAX_IMAGE_BYTES // 256)
    client = client_for(ray_serve.url, presents=ray_serve.token(), picture=picture)

    answer = _ask(client, ray_model.DETECTOR, prompt="a seal")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "model": ray_model.DETECTOR,
        "version": ray_model.VERSION,
        "proposals": [
            {
                "shape_type": "bbox",
                "x": 10.0,
                "y": 20.0,
                "width": 30.0,
                "height": 40.0,
                "polygon": [],
                "label": "seal",
                "text": hashlib.sha256(picture).hexdigest(),
                "score": ray_model.DETECTOR_SCORE,
            }
        ],
        "dropped": [],
    }


def _extended(token: str) -> str:
    """``token`` with its expiry moved an hour ahead and its signature kept: what a caller presents who altered an expired
    token. Only the signature can refuse it."""
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["exp"] = int(time.time()) + 3600
    altered = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{head}.{altered}.{signature}"


#: What the person reads when the deployment refuses the token the annotator presents.
REFUSED = "the model endpoint refused the workbench's own identity ({})"


@ON_RAY
@pytest.mark.parametrize(
    ("presenting", "status", "detail"),
    [
        pytest.param("the-annotators", 200, None, id="the-annotators-token"),
        pytest.param("after-a-rotation", 200, None, id="a-token-the-issuers-new-key-signed"),
        pytest.param("expired", 502, REFUSED.format(401), id="an-expired-token"),
        pytest.param("never-expires", 502, REFUSED.format(401), id="a-token-with-no-expiry"),
        pytest.param("another-audience", 502, REFUSED.format(401), id="a-token-for-another-audience"),
        pytest.param("another-issuer", 502, REFUSED.format(401), id="a-token-naming-another-issuer"),
        pytest.param("tampered", 502, REFUSED.format(401), id="a-token-whose-claims-were-changed"),
        pytest.param("another-workload", 502, REFUSED.format(403), id="another-workloads-token"),
    ],
)
def test_the_ray_serve_deployment_takes_the_annotators_token_and_refuses_any_other(
    client_for: Build, ray_serve: RayServe, presenting: str, status: int, detail: str | None
) -> None:
    if presenting == "after-a-rotation":
        ray_serve.issuer.rotate()
    tokens = {
        "the-annotators": ray_serve.token,
        "after-a-rotation": ray_serve.token,
        # Past the deployment's 30 s of skew.
        "expired": lambda: ray_serve.token(lifetime=-120),
        "never-expires": lambda: ray_serve.token(omit=["exp"]),
        "another-audience": lambda: ray_serve.token(audience="another-service"),
        "another-issuer": lambda: ray_serve.token(iss="https://another-issuer.example"),
        "tampered": lambda: _extended(ray_serve.token(lifetime=-120)),
        # Signed by the cluster's issuer for the deployment's audience: any pod may project such a token.
        "another-workload": lambda: ray_serve.token("system:serviceaccount:other:intruder"),
    }
    client = client_for(ray_serve.url, presents=tokens[presenting]())

    answer = _ask(client, ray_model.DETECTOR, prompt="a seal")

    assert (answer.status_code, answer.json().get("detail")) == (status, detail), answer.text


def test_a_deployment_with_no_endpoint_lists_assist_as_unavailable_with_the_reason(client_for: Build) -> None:
    client = client_for(None)

    listing = client.get("/api/assist/models", params={"project": "p1", "task_id": "t1"})

    assert (listing.status_code, listing.json()) == (200, {"available": False, "reason": UNAVAILABLE, "models": []})


def test_an_ask_in_a_deployment_with_no_endpoint_is_refused_as_unavailable(client_for: Build) -> None:
    client = client_for(None)

    answer = _ask(client, stand_in.DETECTOR, prompt="a seal")

    assert (answer.status_code, answer.json()["type"], answer.json()["detail"]) == (503, "about:blank#no-model-endpoint", UNAVAILABLE)


@respx.mock
def test_an_endpoint_that_answers_more_than_the_workbench_reads_is_an_error_and_its_answer_is_not_held(client_for: Build) -> None:
    client = client_for("http://models.test")
    # The length the endpoint declares is past the cap: the answer is refused before a byte of it is read.
    respx.post("http://models.test/v1/proposals").mock(return_value=httpx.Response(200, headers={"content-length": str(MAX_ANSWER_BYTES + 1)}, content=b"{}"))

    answer = _ask(client, stand_in.DETECTOR, prompt="a seal")

    assert (answer.status_code, answer.json()["detail"]) == (502, "the model endpoint answered more than the workbench reads")


def _proposal(shape_id: str) -> Shape:
    """A proposal as assist's answer is saved, written by no person: its `reviewer` is nobody until someone keeps it."""
    return Shape(
        shape_id=shape_id, shape_type="bbox", x=1, y=1, width=2, height=2, label="seal", status="prediction", source=f"model:{stand_in.DETECTOR}",
        model_version=stand_in.VERSION, confidence=stand_in.DETECTOR_SCORE,
    )  # fmt: skip


def _hold(client: TestClient, shapes: list[Shape], *, base: int = 0, reviewer: str | None = None) -> int:
    """Put ``shapes`` in task t1's work through the store, as its annotator or as ``reviewer`` of it in review, and answer
    the work's revision."""
    app, portal = cast("FastAPI", client.app), client.portal
    assert portal is not None, "the client is not running the app"
    change = app.state.store.change_draft
    return portal.call(
        lambda: change("t1", author=reviewer or "anon", change=lambda _draft, _now: (shapes, []), base_revision=base, reviewing=reviewer is not None)
    ).revision


def _fire(client: TestClient, event: str) -> httpx.Response:
    return client.post("/tasks/t1/events", params={"project": "p1"}, json={"event": event})


def test_an_item_holding_proposals_nobody_settled_is_refused_at_submit_with_the_count(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)
    _hold(client, [_proposal("s0"), _proposal("s1")])

    refused = _fire(client, "submit")

    assert (refused.status_code, refused.headers["content-type"]) == (409, "application/problem+json"), refused.text
    assert refused.json()["detail"] == "2 proposals on this item are neither kept nor removed: keep or remove each before submitting"


def test_a_proposal_a_person_rejected_does_not_stand_for_the_items_work_at_submit(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)
    _hold(client, [_proposal("s0").model_copy(update={"status": "rejected"})])

    refused = _fire(client, "submit")

    # The item's classes ask for at least one label, and a shape a person turned down is none.
    assert refused.status_code == 409, refused.text
    assert "a submission must carry at least one annotation" in refused.json()["detail"]


def test_an_item_whose_proposals_were_all_kept_is_submitted(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)
    revision = _hold(client, [_proposal("s0"), _proposal("s1")])
    kept = client.post(f"/api/annotations/{UNIT}/proposals", params=WORK, json={"action": "keep", "base_version": revision})
    assert (kept.status_code, kept.json()) == (200, {"settled": 2, "version": revision + 1}), kept.text

    submitted = _fire(client, "submit")

    assert (submitted.status_code, submitted.json()["state"]) == (200, "in_review"), submitted.text


def test_a_reviewer_cannot_accept_an_item_that_gained_a_proposal_in_review(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)
    person_made = Shape(shape_id="b0", shape_type="bbox", x=1, y=1, width=2, height=2, label="seal", source="human")
    revision = _hold(client, [person_made])
    assert _fire(client, "submit").status_code == 200
    # Another person reviews: the one who submitted may neither change nor accept their own work in review.
    _hold(client, [person_made, _proposal("s0")], base=revision, reviewer="rita")
    cast("FastAPI", client.app).dependency_overrides[current_subject] = lambda: "rita"

    refused = _fire(client, "accept")

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "1 proposal on this item is neither kept nor removed: keep or remove it before accepting"


@pytest.mark.parametrize(
    ("action", "left"),
    [
        # A kept proposal is a label a person stands behind, and still says which model made it, at which version and score.
        pytest.param("keep", [("accepted", f"model:{stand_in.DETECTOR}", stand_in.VERSION, stand_in.DETECTOR_SCORE, "anon")], id="keep"),
        pytest.param("remove", [], id="remove"),
    ],
)
def test_every_proposal_of_an_item_is_kept_or_removed_in_one_action(client_for: Build, endpoint: str, action: str, left: list[tuple[object, ...]]) -> None:
    client = client_for(endpoint)
    revision = _hold(client, [_proposal("s0")])

    settled = client.post(f"/api/annotations/{UNIT}/proposals", params=WORK, json={"action": action, "base_version": revision})

    shapes = client.get("/tasks/t1/draft", params={"project": "p1"}).json()["shapes"]
    assert (settled.status_code, settled.json()) == (200, {"settled": 1, "version": revision + 1}), settled.text
    assert [(s["status"], s["source"], s["model_version"], s["confidence"], s["reviewer"]) for s in shapes] == left


def test_a_model_is_asked_only_about_the_tasks_own_unit(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint)

    another_unit = client.post(f"/api/assist/{DOC}/0/20", params=QUERY, json={"model": stand_in.DETECTOR, "task_id": "t1"})

    assert (another_unit.status_code, another_unit.json()["detail"]) == (404, f"unit {DOC}/0/20 of dataset {DATASET} is not this item's")
    assert _ASKS == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/api/assist/models", None, id="the-models"),
        pytest.param("POST", f"/api/assist/{UNIT}", {"model": stand_in.DETECTOR, "task_id": "t1"}, id="an-ask"),
    ],
)
def test_someone_who_does_not_annotate_the_project_is_refused_at_every_assist_door(
    client_for: Build, endpoint: str, method: str, path: str, body: dict[str, str] | None
) -> None:
    client = client_for(endpoint)
    asked: list[tuple[str, str, str]] = []

    async def deny(*, user: str, relation: str, obj: str, contextual: object = ()) -> bool:
        asked.append((user, relation, obj))
        return False

    cast("FastAPI", client.app).dependency_overrides[get_checker] = lambda: deny

    refused = client.request(method, path, params=QUERY, json=body)

    assert (refused.status_code, asked[0]) == (403, ("anon", "can_annotate", "project:p1")), refused.text
    assert _ASKS == []


@pytest.mark.parametrize(
    ("tools", "kept", "dropped"),
    [
        pytest.param(["bbox"], [], ["1 polygon proposal(s): this item's classes take bbox"], id="classes-that-take-boxes-only"),
        pytest.param([], ["polygon"], [], id="classes-that-hold-shapes-to-no-type"),
    ],
)
def test_a_proposal_of_a_shape_the_tasks_classes_do_not_take_is_refused_and_said(
    client_for: Build, endpoint: str, tools: list[str], kept: list[str], dropped: list[str]
) -> None:
    client = client_for(endpoint, tools=tools)

    answer = _ask(client, stand_in.SEGMENTER, points=[{"x": 200, "y": 200}])

    assert (answer.status_code, [p["shape_type"] for p in answer.json()["proposals"]], answer.json()["dropped"]) == (200, kept, dropped)


def test_a_model_that_takes_no_prompt_is_asked_with_the_item_alone(client_for: Build, endpoint: str) -> None:
    client = client_for(endpoint, tools=["tag"])

    answer = _ask(client, stand_in.TAGGER)

    (ask,) = [seen for seen in _ASKS if seen["path"] == "/v1/proposals"]
    assert cast("dict[str, Any]", ask["body"])["prompts"] == {"text": None, "box": None, "points": []}
    assert (answer.status_code, [(p["shape_type"], p["label"], p["score"]) for p in answer.json()["proposals"]]) == (
        200,
        [("tag", "seal", stand_in.TAGGER_SCORE)],
    )


def test_the_published_protocol_is_the_one_the_stand_in_speaks() -> None:
    published = (Path(__file__).parents[3] / "docs" / "model-protocol-openapi.json").read_text()

    assert stand_in.published_protocol() == published, "docs/model-protocol-openapi.json is not what the stand-in serves: run `make model-protocol`"
