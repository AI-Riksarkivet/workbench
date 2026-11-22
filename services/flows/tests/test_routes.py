"""The flows HTTP contracts: the catalog's shape, validation, and a run started, driven and read back.

The real flows app with its real lifespan, on a Postgres database of the test's own (`conftest.py::client`), so a run
is recorded, driven by the service's own run driver and read back from its rows exactly as a deployment does. Serve is
mocked at the TRANSPORT (respx).

`/api` is the mount prefix on purpose: it is what the chart and `scripts/local-stack.sh` set `WORKBENCH_API_PREFIX` to.
"""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from flows import health, routes
from flows.lifespan import lifespan
from tests.flows_runs import ServeStandIn, ended, until
from tests.postgres import StateDatabase
from workbench.app import make_service_app


SERVE = "http://serve.invalid:8000"

ALTO = '<TextLine><String CONTENT="Anno"/><String CONTENT="1723"/></TextLine>'

#: text → model → alto → inspect, the v0 chain the studio palette can draw.
CHAIN: dict[str, Any] = {
    "graph": {
        "nodes": [
            {"id": "t", "kind": "text"},
            {"id": "m", "kind": "model", "config": {"app": "htrflow"}},
            {"id": "a", "kind": "alto"},
            {"id": "s", "kind": "inspect"},
        ],
        "edges": [
            {"source": "t", "target": "m"},
            {"source": "m", "target": "a"},
            {"source": "a", "target": "s"},
        ],
    },
    "seeds": {"t": "page bytes stand-in"},
}


def _start(client: TestClient, body: dict[str, Any], **headers: str) -> dict[str, Any]:
    response = client.post("/api/flows/projects/p1/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


# ---- catalog ---------------------------------------------------------------------------------


def test_the_catalog_endpoint_serves_the_pinned_shape(client: TestClient) -> None:
    resp = client.get("/api/flows/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"version", "kinds"}
    assert body["version"] == 1
    assert {k["kind"] for k in body["kinds"]} == {
        "image",
        "text",
        "dataset",
        "prompt",
        "model",
        "mcp",
        "alto",
        "extract",
        "regex",
        "compare",
        "inspect",
    }
    assert set(body["kinds"][0]) == {"kind", "label", "role", "params"}


# ---- validate --------------------------------------------------------------------------------


def test_validate_accepts_the_v0_chain(client: TestClient) -> None:
    resp = client.post("/api/flows/projects/p1/validate", json=CHAIN["graph"])
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "problems": []}


def test_validate_refuses_a_cycle_and_says_why(client: TestClient) -> None:
    graph = {
        "nodes": [{"id": "a", "kind": "text"}, {"id": "b", "kind": "inspect"}],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    }
    body = client.post("/api/flows/projects/p1/validate", json=graph).json()
    assert body["ok"] is False
    assert body["problems"] == ["graph has a cycle"]


# ---- runs ------------------------------------------------------------------------------------


def test_a_run_is_accepted_driven_to_its_end_and_readable_afterwards(client: TestClient, respx_mock: respx.MockRouter) -> None:
    """202 with the run as recorded, then the run driven in the background and read back from its rows, each step's
    outcome on its node."""
    serve = respx_mock.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    started = _start(client, CHAIN)
    run = ended(client, "p1", started["run_id"])

    assert started["status"] == "running"
    assert run["status"] == "succeeded"
    assert run["project"] == "p1"
    assert run["nodes"]["s"]["output_text"] == "Anno 1723"
    assert serve.call_count == 1
    assert serve.calls.last.request.content == b"page bytes stand-in"
    assert serve.calls.last.request.headers["idempotency-key"] == f"{run['run_id']}:m"


def test_a_failing_model_node_blocks_its_dependents(client: TestClient, respx_mock: respx.MockRouter) -> None:
    """A 502 from Serve fails the model node; `alto` and `inspect` are recorded blocked and never executed. The run
    ends `failed`, with every node painted: a sandbox that hid its red node would leave the builder nothing to paint."""
    respx_mock.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(502))

    run = ended(client, "p1", _start(client, CHAIN)["run_id"])

    nodes = run["nodes"]
    assert run["status"] == "failed"
    assert nodes["t"]["status"] == "succeeded"
    assert nodes["m"]["error"] == "serve returned 502 for app 'htrflow' (3 attempts)"
    assert nodes["a"]["error"] == "upstream failed"
    assert nodes["s"]["error"] == "upstream failed"
    assert nodes["a"]["ms"] is None


def test_a_downstream_node_receives_the_full_payload_not_the_display_truncation(client: TestClient, respx_mock: respx.MockRouter) -> None:
    """`NodeRunState.output_text` is capped for display at 4 000 characters; the payload a step hands its dependents is
    stored whole (up to its own, far higher, ceiling) and read back as their input. Feeding the display copy downstream
    would hand an alto node a document cut mid-element."""
    long_alto = "<TextLine>" + "".join(f'<String CONTENT="w{i}"/>' for i in range(1200)) + "</TextLine>"
    respx_mock.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=long_alto))

    run = ended(client, "p1", _start(client, CHAIN)["run_id"])

    assert run["status"] == "succeeded"
    assert len(run["nodes"]["m"]["output_text"]) == 4000  # the display cap held on the model node
    # 4000 characters of that XML cover roughly the first 170 `<String>` elements, so a `w500` in the alto node's text
    # is only possible if it was fed the UNCAPPED payload.
    assert "w500" in run["nodes"]["a"]["output_text"].split()


def test_a_graph_that_does_not_validate_is_refused_with_the_problem_LIST(client: TestClient) -> None:
    """422 + problem+json + the structured `problems`. The list is the point: the builder highlights
    the offending nodes, which a single flattened `detail` string cannot support."""
    resp = client.post(
        "/api/flows/projects/p1/runs",
        json={"graph": {"nodes": [{"id": "a", "kind": "quantum"}], "edges": []}, "seeds": {}},
        headers={"Idempotency-Key": "refused"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["problems"] == ["unknown node kind: quantum (node a)"]


def test_the_same_idempotency_key_resolves_to_the_same_run_and_executes_once(client: TestClient, respx_mock: respx.MockRouter) -> None:
    """A retried start with the caller's key finds the run the first one recorded and starts nothing, and says it is a
    replay (`Idempotent-Replayed: true`, 200 rather than the fresh accept's 202); the same key on a different request is
    refused with 422 rather than answered with the other run (draft-ietf-httpapi-idempotency-key-header § 2.7)."""
    serve = respx_mock.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    first = client.post("/api/flows/projects/p1/runs", json=CHAIN, headers={"Idempotency-Key": "page-7"})
    ended(client, "p1", first.json()["run_id"])
    second = client.post("/api/flows/projects/p1/runs", json=CHAIN, headers={"Idempotency-Key": "page-7"})
    other = client.post("/api/flows/projects/p1/runs", json={**CHAIN, "seeds": {"t": "another page"}}, headers={"Idempotency-Key": "page-7"})

    assert (first.status_code, first.headers.get("idempotent-replayed")) == (202, None)
    assert (second.status_code, second.headers.get("idempotent-replayed")) == (200, "true")
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["status"] == "succeeded"
    assert other.status_code == 422, other.text
    assert serve.call_count == 1

    # The replay is judged on the request as sent, not on the run's stored copy: a float past 2**53 comes back from the
    # store as another number, and an identical replay must still be a replay.
    big = '{"graph": {"nodes": [{"id": "t", "kind": "text", "config": {"x": 1.2345678901234567e30}}], "edges": []}, "seeds": {"t": "a"}}'
    keyed = {"Idempotency-Key": "big-float", "content-type": "application/json"}
    client.post("/api/flows/projects/p1/runs", content=big, headers=keyed)
    assert client.post("/api/flows/projects/p1/runs", content=big, headers=keyed).status_code == 200


def test_a_run_holding_a_value_the_run_store_cannot_keep_is_refused_at_the_door(client: TestClient) -> None:
    """Postgres stores no NUL in text or jsonb and no non-finite number in jsonb, and a config nested past its bound
    cannot be walked: each is refused with 422 naming the reason, rather than failing the door with a 500."""
    seed = client.post("/api/flows/projects/p1/runs", json={**CHAIN, "seeds": {"t": "a\u0000b"}})
    nodes = [{**node, "config": {"app": "ht\u0000rflow"}} if node["id"] == "m" else node for node in CHAIN["graph"]["nodes"]]
    config = client.post("/api/flows/projects/p1/runs", json={**CHAIN, "graph": {**CHAIN["graph"], "nodes": nodes}})
    raw = '{"graph": {"nodes": [{"id": "t", "kind": "text", "config": {"x": %s}}], "edges": []}, "seeds": {"t": "a"}}'
    non_finite = [
        client.post("/api/flows/projects/p1/runs", content=raw % literal, headers={"content-type": "application/json"}) for literal in ("NaN", "1e400")
    ]
    deep = client.post("/api/flows/projects/p1/runs", content=raw % ("[" * 900 + "]" * 900), headers={"content-type": "application/json"})
    # A key the store indexes past what its index holds (a node id is half of a step's primary key), or a key nobody
    # needs that long, is refused at the door.
    long_id = client.post("/api/flows/projects/p1/runs", json={"graph": {"nodes": [{"id": "n" * 4000, "kind": "text"}], "edges": []}, "seeds": {}})
    long_key = client.post("/api/flows/projects/p1/runs", json=CHAIN, headers={"Idempotency-Key": "k" * 4000})

    assert (seed.status_code, config.status_code) == (422, 422), (seed.text, config.text)
    assert "NUL" in seed.text
    assert [r.status_code for r in non_finite] == [422, 422], [r.text for r in non_finite]
    assert "finite" in non_finite[0].text
    assert deep.status_code == 422, deep.text[:300]
    assert "nests deeper" in deep.text
    assert (long_id.status_code, long_key.status_code) == (422, 422), (long_id.text[:300], long_key.text[:300])


def test_a_step_whose_output_the_run_store_cannot_keep_fails_with_its_reason(client: TestClient) -> None:
    """A step can PRODUCE text Postgres cannot store (an extract of a JSON string holding `\\u0000`). The step fails
    with that reason and the run ends; it is not left running for every re-drive to execute the step again."""
    body = {
        "graph": {
            "nodes": [{"id": "t", "kind": "text"}, {"id": "x", "kind": "extract", "config": {"extractPath": "a"}}],
            "edges": [{"source": "t", "target": "x"}],
        },
        "seeds": {"t": '{"a": "\\u0000"}'},
    }

    run = ended(client, "p1", _start(client, body)["run_id"])

    assert run["status"] == "failed"
    assert "NUL" in (run["nodes"].get("x") or {}).get("error", ""), run


def test_a_key_less_post_still_gets_a_fresh_run_each_time(client: TestClient, respx_mock: respx.MockRouter) -> None:
    """No key means nothing to converge ON. Inventing one would make every retry a new run while
    pretending otherwise, so the randomness stays in the key and the id derivation stays single."""
    respx_mock.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    ids = {_start(client, CHAIN)["run_id"] for _ in range(3)}

    assert len(ids) == 3
    assert all(ended(client, "p1", run_id)["status"] == "succeeded" for run_id in ids)


def test_a_cancelled_run_executes_no_step_it_had_not_started(serve: ServeStandIn, flows_state: StateDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel ends the run at once; the step in flight runs to its end and its outcome is not recorded, and the step
    after it never runs."""
    serve.hold("first")
    two_models = {
        "graph": {
            "nodes": [
                {"id": "t", "kind": "text"},
                {"id": "m1", "kind": "model", "config": {"app": "first"}},
                {"id": "m2", "kind": "model", "config": {"app": "second"}},
            ],
            "edges": [{"source": "t", "target": "m1"}, {"source": "m1", "target": "m2"}],
        },
        "seeds": {"t": "page"},
    }
    monkeypatch.setenv("WORKBENCH_API_PREFIX", "/api")
    with TestClient(make_service_app(title="flows", routers=[routes.router], lifespan=lifespan)) as client:
        run_id = _start(client, two_models)["run_id"]
        until(lambda: serve.reached("first"), "the first model step was never started")

        cancelled = client.post(f"/api/flows/projects/p1/runs/{run_id}/cancel")
        serve.release("first")
        until(lambda: not client.app.state.run_runner.driving(), "the driver did not stop")
        after = client.get(f"/api/flows/projects/p1/runs/{run_id}").json()

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert after["status"] == "cancelled"
    assert set(after["nodes"]) == {"t"}, f"a step was recorded after the cancel: {after['nodes']}"
    assert serve.calls_to("second") == [], "a step the run had not started ran after the cancel"


def test_a_run_id_of_another_shape_is_refused_on_read_and_cancel(client: TestClient) -> None:
    """A run id is `run-` and 32 hex digits (`run_id_for`); anything else, a NUL included, is refused before it reaches
    the store, never answered with a 500."""
    answers = [
        client.get("/api/flows/projects/p1/runs/a%00b"),
        client.post("/api/flows/projects/p1/runs/a%00b/cancel"),
        client.get("/api/flows/projects/p1/runs/run-x"),
    ]

    assert [answer.status_code for answer in answers] == [422, 422, 422], [answer.text[:200] for answer in answers]


def test_the_run_doors_answer_503_on_a_process_with_no_run_store(monkeypatch: pytest.MonkeyPatch, flows_state: StateDatabase) -> None:
    """No run store named, no runs: the doors say why, and validation still serves."""
    monkeypatch.delenv("WORKBENCH_FLOWS_STATE_HOST")
    monkeypatch.setenv("WORKBENCH_API_PREFIX", "/api")
    with TestClient(make_service_app(title="flows", routers=[health.router, routes.router], lifespan=lifespan)) as bare:
        refused = bare.post("/api/flows/projects/p1/runs", json=CHAIN)
        validated = bare.post("/api/flows/projects/p1/validate", json=CHAIN["graph"])

    assert refused.status_code == 503
    assert "WORKBENCH_FLOWS_STATE_HOST" in refused.json()["detail"]
    assert validated.status_code == 200


def test_an_oversized_graph_is_REFUSED_at_the_door_with_the_node_NAMED(client: TestClient) -> None:
    """The bound has to reach the 422 BODY, not just the pure function.

    `validate_graph`'s new size checks are worth nothing to a caller unless they arrive as
    `RunRefused.problems` — that list is what the builder paints, and a run refused with an empty
    list is indistinguishable from a run refused for a reason nobody can see.

    Driven at the fan-in bound rather than the node cap because it is the reachable one: two nodes
    and a repeatedly-dragged edge, which is how a real canvas produces it. No Serve mock is
    registered on purpose — a graph refused at the door must never reach a step.
    """
    from flows.models import MAX_NODE_FAN_IN

    over = MAX_NODE_FAN_IN + 1
    resp = client.post(
        "/api/flows/projects/p1/runs",
        json={
            "graph": {
                "nodes": [{"id": "a", "kind": "text"}, {"id": "b", "kind": "inspect"}],
                "edges": [{"source": "a", "target": "b"} for _ in range(over)],
            },
            "seeds": {"a": "hello"},
        },
    )

    assert resp.status_code == 422, f"an over-wide graph was accepted: {resp.status_code}"
    body = resp.json()
    assert body["status"] == 422
    assert body["problems"], "refused with an EMPTY problems list — the builder has nothing to paint"
    assert any("b" in p and str(MAX_NODE_FAN_IN) in p for p in body["problems"]), f"the 422 must name the offending node AND the ceiling: {body['problems']}"
