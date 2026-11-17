"""A run outlives the process driving it, and every step's outcome is recorded once.

Each run and each step's outcome are rows in the service's own Postgres, and a driver holds a run under a lease
(`flows.runner`). These run the real thing: the flows service under a real uvicorn, killed with SIGKILL in the middle of
a run, a second process started on the same database, and a Serve stand-in on the loopback that records every call by
its `Idempotency-Key`. And two drivers sweeping one database at the same moment.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from flows import runner as runner_module
from flows.config import FlowsSettings
from flows.models import FlowEdge, FlowGraph, FlowNode, NodeResult, NodeRunState, RunJob
from flows.runner import RunRunner, step_key
from flows.store import FlowsStore
from tests.flows_runs import ServeStandIn, ended, state_env, until
from tests.postgres import StateDatabase


#: text → first → second → inspect: two model steps, so a driver can die after one is recorded and inside the other.
TWO_STEPS: dict[str, Any] = {
    "graph": {
        "nodes": [
            {"id": "t", "kind": "text"},
            {"id": "m1", "kind": "model", "config": {"app": "first"}},
            {"id": "m2", "kind": "model", "config": {"app": "second"}},
            {"id": "s", "kind": "inspect"},
        ],
        "edges": [{"source": "t", "target": "m1"}, {"source": "m1", "target": "m2"}, {"source": "m2", "target": "s"}],
    },
    "seeds": {"t": "page"},
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _flows(environment: dict[str, str]) -> Generator[tuple[subprocess.Popen[bytes], httpx.Client]]:
    """The flows service under a real uvicorn, on a port of its own, and a client of it; stopped by its PID."""
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - argv is the interpreter and literals
        [sys.executable, "-m", "uvicorn", "flows:app", "--host", "127.0.0.1", "--port", str(port)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
    try:

        def live() -> bool:
            if process.poll() is not None:
                raise AssertionError(f"flows exited with {process.returncode} while starting")
            try:
                return client.get("/livez").status_code == 200
            except httpx.TransportError:
                return False

        until(live, "flows did not answer its liveness probe")
        yield process, client
    finally:
        client.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=20)


def test_a_run_whose_driver_is_killed_is_resumed_and_runs_each_step_once(serve: ServeStandIn, state_database: StateDatabase, tmp_path: Path) -> None:
    """Killed after the first model step was recorded and inside the second, the run is resumed by the next process:
    the first step is not executed again, the second is executed again with the same key, and the run ends with each
    step's outcome once."""
    token = tmp_path / "serve-token"
    token.write_text("flows-sa-token")
    environment = {
        **os.environ,
        **state_env(state_database),
        "WORKBENCH_API_PREFIX": "/api",
        "WORKBENCH_FLOWS_SERVE_URL": serve.url,
        "WORKBENCH_FLOWS_SERVE_TOKEN_FILE": str(token),
    }
    serve.hold("second")

    with _flows(environment) as (first_process, first):
        started = first.post("/api/flows/projects/p1/runs", json=TWO_STEPS, headers={"Idempotency-Key": "page-1"})
        run_id = started.json()["run_id"]
        until(lambda: serve.reached("second"), "the second model step was never started")
        first_process.send_signal(signal.SIGKILL)
        first_process.wait(timeout=20)
    serve.release("second")

    with _flows(environment) as (_, second):
        run = ended(second, "p1", run_id)

    assert started.status_code == 202
    assert run["status"] == "succeeded", run
    assert run["nodes"]["s"]["output_text"] == "second(first(page))"
    assert serve.calls_to("first") == [step_key(run_id, "m1")], "a step recorded before the kill was executed again"
    assert serve.calls_to("second") == [step_key(run_id, "m2")] * 2, "the step the kill interrupted was not executed again with its own key"


@pytest.mark.asyncio
async def test_two_drivers_sweeping_one_database_at_once_never_drive_one_run_twice(serve: ServeStandIn, flows_state: StateDatabase) -> None:
    """Runs no driver holds, swept by two replicas at the same moment: each run is driven by one of them, and each
    model step of each run calls Serve once."""
    settings = FlowsSettings.model_validate({})
    stores = [await FlowsStore.open(settings), await FlowsStore.open(settings)]
    graph = FlowGraph(nodes=[FlowNode(id="t", kind="text"), FlowNode(id="m", kind="model", config={"app": "first"})], edges=[FlowEdge(source="t", target="m")])
    job = RunJob(graph=graph, seeds={"t": "page"}, serve_url=settings.serve_url, serve_token_file=settings.serve_token_file, project="p1")
    async with httpx.AsyncClient() as client:
        runners = [RunRunner(store, client, lease_seconds=30, redrive_seconds=30, concurrency=8, max_attempts=5, retention_days=30) for store in stores]
        try:
            run_ids = [f"run-{i}" for i in range(6)]
            for run_id in run_ids:
                await stores[0].create(run_id, subject="anon", job=job)

            claimed = await asyncio.gather(*(runner.sweep() for runner in runners))
            await asyncio.to_thread(until, lambda: not any(runner.driving() for runner in runners), "the drivers did not finish")
            ended_runs = [await stores[0].get(run_id) for run_id in run_ids]
        finally:
            for runner in runners:
                await runner.close()
            for store in stores:
                await store.close()

    assert sorted(claimed[0] + claimed[1]) == run_ids, f"a run was claimed by both drivers or by neither: {claimed}"
    assert sorted(str(key) for key in serve.calls_to("first")) == sorted(step_key(run_id, "m") for run_id in run_ids), "a step ran twice, or never"
    assert [run.state if run else None for run in ended_runs] == ["succeeded"] * len(run_ids)


async def _eventually(check: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(what)
        await asyncio.sleep(0.05)


def _one_model_job(settings: FlowsSettings, app: str) -> RunJob:
    graph = FlowGraph(nodes=[FlowNode(id="t", kind="text"), FlowNode(id="m", kind="model", config={"app": app})], edges=[FlowEdge(source="t", target="m")])
    return RunJob(graph=graph, seeds={"t": "page"}, serve_url=settings.serve_url, serve_token_file=settings.serve_token_file, project="p1")


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["refused", "hung"])
async def test_a_driver_keeps_its_lease_through_one_failed_renewal_and_stops_its_step_once_the_lease_is_lost(
    serve: ServeStandIn, flows_state: StateDatabase, lost: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One renewal the database refuses does not cost a healthy driver its run. A driver that cannot renew past its
    lease — every renewal refused, or one that never answers (a partition, a dead TCP peer) — stops the step in flight
    before the lease lapses and records nothing, so it never runs a step beside the driver that resumes it."""
    settings = FlowsSettings.model_validate({})
    store, other_store = await FlowsStore.open(settings), await FlowsStore.open(settings)
    await store.create("run-x", subject="anon", job=_one_model_job(settings, "slow"))
    lease = 3.0
    loop = asyncio.get_running_loop()
    renew, drive, failures, never = store.renew, runner_module.drive_run, {"left": 1}, asyncio.Event()
    #: When the last renewal that got through was ASKED (the database stamps the lease no earlier), and when the drive,
    #: the step in flight with it, was stopped: a stop before the first moment the lease could lapse proves the driver
    #: never ran its step beside a rival. Event times, so no poll's latency is measured.
    seen: dict[str, float] = {}

    async def flaky_renew(run_id: str, owner: str, lease_seconds: float) -> bool:
        if failures["left"] > 0:
            failures["left"] -= 1
            if lost == "hung" and failures["left"] > 1:
                await never.wait()
            raise OSError("the database refused one renewal")
        asked = loop.time()
        held = await renew(run_id, owner, lease_seconds)
        seen["renewed"] = asked
        return held

    async def recorded_drive(*args: Any, **kwargs: Any) -> object:
        try:
            return await drive(*args, **kwargs)
        except asyncio.CancelledError:
            seen.setdefault("stopped", loop.time())
            raise

    store.renew = flaky_renew  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(runner_module, "drive_run", recorded_drive)
    serve.hold("slow")
    async with httpx.AsyncClient() as client:
        driver = RunRunner(store, client, lease_seconds=lease, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        rival = RunRunner(other_store, client, lease_seconds=lease, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        try:
            assert await driver.sweep() == ["run-x"]
            await asyncio.sleep(2 * lease)
            taken_while_healthy = await rival.sweep()

            failures["left"] = 10**6
            # Serve is still holding the step's call, so a driver that stops now stopped the step in flight.
            await _eventually(lambda: not driver.driving(), "a driver that lost its lease kept running its step")
            recorded = await other_store.get("run-x")
        finally:
            never.set()
            serve.release("slow")
            await driver.close()
            await rival.close()
            await store.close()
            await other_store.close()

    assert taken_while_healthy == [], "a healthy driver lost its run after one failed renewal"
    assert recorded is not None and set(recorded.steps) == {"t"}, "a driver that lost its lease recorded its step"
    assert seen["stopped"] < seen["renewed"] + lease, (
        f"the driver stopped its step {seen['stopped'] - seen['renewed']:.2f} s after its last renewal was asked, past its {lease} s lease"
    )


@pytest.mark.asyncio
async def test_a_driver_whose_renewals_answer_slowly_keeps_its_run(serve: ServeStandIn, flows_state: StateDatabase) -> None:
    """Renewals that each answer after 30% of the lease (a loaded database) are slow but inside the lease: the next one
    is asked a tick after the last was asked, not after it answered, so the driver keeps its run."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    await store.create("run-x", subject="anon", job=_one_model_job(settings, "slow"))
    lease, renew = 3.0, store.renew

    async def slow_renew(run_id: str, owner: str, lease_seconds: float) -> bool:
        await asyncio.sleep(0.3 * lease)
        return await renew(run_id, owner, lease_seconds)

    store.renew = slow_renew  # ty: ignore[invalid-assignment]
    serve.hold("slow")
    async with httpx.AsyncClient() as client:
        driver = RunRunner(store, client, lease_seconds=lease, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        try:
            await driver.sweep()
            await asyncio.sleep(4 * lease)
            kept = driver.driving()
        finally:
            serve.release("slow")
            await driver.close()
            await store.close()

    assert kept == {"run-x"}, "a driver whose renewals answered inside its lease lost its run"


@pytest.mark.asyncio
async def test_a_sweep_never_claims_a_run_its_own_process_drives(serve: ServeStandIn, flows_state: StateDatabase) -> None:
    """A drive whose lease has lapsed in the store while it still runs is not claimed again by its own process's sweep,
    which would count an attempt for nothing and start a second drive of one run."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    await store.create("run-x", subject="anon", job=_one_model_job(settings, "slow"))
    serve.hold("slow")
    async with httpx.AsyncClient() as client:
        driver = RunRunner(store, client, lease_seconds=30, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        try:
            assert await driver.sweep() == ["run-x"]
            await _eventually(lambda: serve.reached("slow"), "the step never started")
            with psycopg.connect(host=flows_state.host, port=flows_state.port, user=flows_state.user, dbname=flows_state.database, autocommit=True) as conn:
                conn.execute("UPDATE runs SET lease_until = clock_timestamp() - interval '1 second' WHERE run_id = 'run-x'")
            again = await driver.sweep()
            run = await store.get("run-x")
        finally:
            serve.release("slow")
            await driver.close()
            await store.close()

    assert again == []
    assert run is not None and run.attempts == 1


@pytest.mark.asyncio
async def test_a_step_outcome_the_store_refuses_ends_the_step_once_with_the_reason(flows_state: StateDatabase) -> None:
    """A store that refuses a step's outcome as DATA (here an incompressible key past the step index's row limit,
    which the door's bounds keep out of real runs) refuses it again on every write: the run ends at once with the database's reason,
    never re-driven toward its attempt bound."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    try:
        await store.create("run-x", subject="anon", job=_one_model_job(settings, "first"))
        assert await store.claim("a-driver", 30, run_id="run-x") == ["run-x"]
        ended = await store.record("run-x", "a-driver", 30, {os.urandom(2000).hex(): NodeResult(state=NodeRunState(status="succeeded"), payload_text="x")})
        run = await store.get("run-x")
    finally:
        await store.close()

    assert ended == "failed"
    assert run is not None and run.state == "failed" and run.attempts == 1
    assert run.error is not None and "ProgramLimitExceeded" in run.error, run.error


@pytest.mark.asyncio
async def test_a_run_started_more_times_than_its_bound_fails_for_good(serve: ServeStandIn, flows_state: StateDatabase) -> None:
    """Every claim counts an attempt. A run whose drivers kept stopping before it ended fails with the reason at the
    claim past the bound, and executes no step: no deterministic failure is re-driven forever."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    await store.create("run-x", subject="anon", job=_one_model_job(settings, "first"))
    for _ in range(3):
        assert await store.claim("a-driver-that-died", 30, run_id="run-x") == ["run-x"]
        await store.release("run-x", "a-driver-that-died")
    async with httpx.AsyncClient() as client:
        runner = RunRunner(store, client, lease_seconds=30, redrive_seconds=60, concurrency=2, max_attempts=3, retention_days=30)
        try:
            await runner.sweep()
            await _eventually(lambda: not runner.driving(), "the driver did not stop")
            run = await store.get("run-x")
        finally:
            await runner.close()
            await store.close()

    assert run is not None and run.state == "failed"
    assert run.error is not None and "3" in run.error, run.error
    assert serve.calls_to("first") == []


@pytest.mark.asyncio
async def test_a_re_driven_run_waits_its_turn_behind_a_fresh_one(flows_state: StateDatabase) -> None:
    """The claim takes the run that waited longest since it was created or last claimed, so a run whose drivers keep
    stopping goes to the back and cannot starve runs that never ran."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    job = _one_model_job(settings, "first")
    try:
        await store.create("run-old", subject="anon", job=job)
        await store.create("run-new", subject="anon", job=job)
        assert await store.claim("a-driver-that-died", 30, limit=1) == ["run-old"]
        await store.release("run-old", "a-driver-that-died")
        next_claim = await store.claim("another-driver", 30, limit=1)
    finally:
        await store.close()

    assert next_claim == ["run-new"]


@pytest.mark.asyncio
async def test_a_finished_run_past_its_retention_is_purged_by_the_sweep(flows_state: StateDatabase) -> None:
    """Every replica's sweep deletes the runs that ended longer ago than the retention, with their steps; a run that
    ended recently and a run still going stay."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    job = _one_model_job(settings, "first")
    for run_id in ("run-aged", "run-recent", "run-going"):
        await store.create(run_id, subject="anon", job=job)
    await store.cancel("run-aged", "p1")
    await store.cancel("run-recent", "p1")
    with psycopg.connect(host=flows_state.host, port=flows_state.port, user=flows_state.user, dbname=flows_state.database, autocommit=True) as conn:
        conn.execute("UPDATE runs SET finished_at = finished_at - interval '31 days' WHERE run_id = 'run-aged'")
        conn.execute("UPDATE runs SET owner = 'x', lease_until = clock_timestamp() + interval '1 hour' WHERE run_id = 'run-going'")
    async with httpx.AsyncClient() as client:
        runner = RunRunner(store, client, lease_seconds=30, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        try:
            await runner.sweep()
            kept = {run_id: await store.get(run_id) is not None for run_id in ("run-aged", "run-recent", "run-going")}
        finally:
            await runner.close()
            await store.close()

    assert kept == {"run-aged": False, "run-recent": True, "run-going": True}


@pytest.mark.asyncio
async def test_a_driver_stopped_by_its_own_shutdown_gives_the_attempt_back(serve: ServeStandIn, flows_state: StateDatabase) -> None:
    """A graceful shutdown (a rolling deploy) stops the step in flight and releases the run without counting the
    attempt, so a long run is never failed by its own deploys."""
    settings = FlowsSettings.model_validate({})
    store = await FlowsStore.open(settings)
    await store.create("run-x", subject="anon", job=_one_model_job(settings, "slow"))
    serve.hold("slow")
    async with httpx.AsyncClient() as client:
        runner = RunRunner(store, client, lease_seconds=30, redrive_seconds=60, concurrency=2, max_attempts=5, retention_days=30)
        try:
            assert await runner.sweep() == ["run-x"]
            await _eventually(lambda: serve.reached("slow"), "the step never started")
            await runner.close()
            run = await store.get("run-x")
        finally:
            serve.release("slow")
            await store.close()

    assert run is not None and run.state == "running"
    assert run.attempts == 0, "a graceful shutdown counted as an attempt"
