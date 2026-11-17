"""Driving flow runs to their end, in every replica, crash-safe without a workflow engine.

A run is driven by one process at a time, under a lease on its row (`flows.store.FlowsStore.claim`). The driver walks
the graph wave by wave (`flows.graph.topo_waves`), executes each step that has no recorded outcome yet, and records each
outcome in the transaction that checks it still holds the run (`FlowsStore.record`). The run ends in the transaction
that records its last step.

| what happens | what follows |
| --- | --- |
| the driver dies between two steps | its lease lapses; any replica's re-drive claims the run and starts at the first step with no outcome |
| the driver dies inside a step | that step has no outcome, so the re-drive executes it again, with the same idempotency key |
| two replicas sweep at once | one claims the run (`FOR UPDATE SKIP LOCKED`); the other finds nothing to claim |
| a driver cannot renew its lease | one failed renewal is tried again; when the lease would lapse first, the step in flight is cancelled and nothing recorded |
| a driver outlives its lease anyway (a hung process) | its next record is refused (`LostLease`) and it stops; whoever holds the run records |
| the run is cancelled | it ends at once; its driver's next renewal or record is refused and it stops |
| the drivers keep stopping before the run ends | every claim counts an attempt; past `max_attempts` the run fails for good, with the reason |
| a run waits behind runs being re-driven | the claim takes the run that waited longest since it was created or last claimed |
| a run ended longer ago than the retention | every replica's sweep deletes it, with its steps |

**A step is safe to run twice.** A step is recorded after it runs, so a driver that dies between the two leaves a step
that ran and has no outcome. The pure kinds (text, prompt, alto, extract, regex, compare, inspect) answer the same
again. A model step calls Serve with `Idempotency-Key: <run id>:<node id>`, the same key on every attempt, so a Serve app
that honours the header answers a repeated call from its record of the first; the same key rides every retry of a call
Serve did not answer yet (`executor.SERVE_ATTEMPTS`). No step is executed again once its outcome is recorded, and a step
whose output the run store cannot keep is recorded as a failure with that reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import uuid
from collections.abc import Coroutine
from typing import Any

import httpx
from opentelemetry import trace

from flows import executor
from flows.graph import topo_waves, upstreams
from flows.metrics import record_node, record_run
from flows.models import NodeJob, NodeResult, NodeRunState, RunStatus
from flows.store import FlowsStore, LostLease


logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("lance.flows")


def step_key(run_id: str, node_id: str) -> str:
    """The idempotency key a step's outbound call carries: the same on every attempt of that step, in any process."""
    return f"{run_id}:{node_id}"


async def drive_run(store: FlowsStore, run_id: str, owner: str, *, client: httpx.AsyncClient, lease_seconds: float, max_attempts: int) -> RunStatus | None:
    """Drive a claimed run until it ends; answer how it ended, or None when the run does not exist.

    A run claimed more than ``max_attempts`` times fails here for good, executing nothing: its drivers kept stopping
    before it ended, and whatever stops them would stop the next one too.

    Raises `LostLease` (inside an `ExceptionGroup` when it came from a step of a wave) when the run stops being this
    driver's.
    """
    run = await store.get(run_id)
    if run is None:
        return None
    if run.state != "running":
        return run.state
    if run.attempts > max_attempts:
        await store.fail(run_id, owner, f"the run was started {max_attempts} times and never ended; it is not started again")
        record_run("failed")
        return "failed"
    job = run.job
    waves = topo_waves(job.graph)
    if waves is None:
        # Refused at the door (`graph.validate_graph`); a stored graph that still cannot be ordered ends the run.
        await store.fail(run_id, owner, "graph has a cycle")
        record_run("failed")
        return "failed"

    by_id = {node.id: node for node in job.graph.nodes}
    incoming = upstreams(job.graph)
    done: dict[str, NodeResult] = dict(run.steps)
    status: RunStatus = "running"

    async def step(node_id: str) -> RunStatus:
        node = by_id[node_id]
        with _tracer.start_as_current_span("flows.node"):
            result = await executor.run_node(
                NodeJob(
                    node=node,
                    inputs=[done[u].payload_text or "" for u in incoming[node_id]],
                    seed=job.seeds.get(node_id),
                    serve_url=job.serve_url,
                    serve_timeout=job.serve_timeout,
                    serve_token_file=job.serve_token_file,
                ),
                client=client,
                idempotency_key=step_key(run_id, node_id),
            )
        recorded = await store.record(run_id, owner, lease_seconds, {node_id: result})
        record_node(result.state.status)
        done[node_id] = result
        return recorded

    for wave in waves:
        pending = [node_id for node_id in wave if node_id not in done]
        if not pending:
            continue
        # A failed step blocks its dependents: they are recorded failed and never executed, so the builder paints the
        # step that broke rather than a second failure about missing input.
        blocked = {
            node_id: NodeResult(state=NodeRunState(status="failed", error="upstream failed"))
            for node_id in pending
            if any(done[u].state.status == "failed" for u in incoming[node_id])
        }
        if blocked:
            status = await store.record(run_id, owner, lease_seconds, blocked)
            done.update(blocked)
        runnable = [node_id for node_id in pending if node_id not in blocked]
        # A wave's steps are independent by construction, so they run at once; a TaskGroup cancels the rest of the wave
        # when one finds the run is no longer this driver's.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(step(node_id)) for node_id in runnable]
        for task in tasks:
            if task.result() != "running":
                status = task.result()

    if status != "running":
        record_run(status)
    return status


class RunRunner:
    """Drives runs in this process, each under a lease on its run's row.

    A run starts when the start door has recorded it (`spawn`), or when this replica's re-drive (`start`, every
    `redrive_seconds` with jitter) finds a running run whose lease is absent or expired: one whose driver died, was
    stopped at a shutdown, or could not reach the database. A driver renews its lease every third of it while it drives,
    and releases it when it stops. At most `concurrency` runs are driven at once in one replica; a run beyond that waits
    for the next sweep, here or in another replica.
    """

    def __init__(
        self,
        store: FlowsStore,
        client: httpx.AsyncClient,
        *,
        lease_seconds: float,
        redrive_seconds: float,
        concurrency: int,
        max_attempts: int,
        retention_days: float,
    ) -> None:
        self._store = store
        self._client = client
        self._lease_seconds = lease_seconds
        self._redrive_seconds = redrive_seconds
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._retention_days = retention_days
        #: The name this process's drivers claim leases under.
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        #: The runs in flight, by id: strong references (the event loop holds only a weak one) and the count the
        #: concurrency bound reads.
        self._runs: dict[str, asyncio.Task[None]] = {}
        #: Slots a sweep has promised to the runs it is claiming.
        self._reserved = 0
        self._loop_task: asyncio.Task[None] | None = None
        #: Set once `close` starts: a drive stopped from then on was stopped by this shutdown.
        self._closing = False

    def driving(self) -> frozenset[str]:
        """The runs this replica drives at this moment."""
        return frozenset(self._runs)

    def start(self) -> None:
        """Start this replica's re-drive: it sweeps at once, then every `redrive_seconds` with jitter."""
        self._loop_task = asyncio.get_running_loop().create_task(self._redrive())

    async def close(self) -> None:
        """Stop the re-drive, cancel the runs in flight and wait for them. Each releases its lease on the way out and
        gives its attempt back, so another replica claims its run at its next sweep and the deploy costs it nothing."""
        self._closing = True
        tasks = [*self._runs.values(), *([self._loop_task] if self._loop_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def spawn(self, run_id: str) -> asyncio.Task[None] | None:
        """Start driving the run the start door just recorded, if this replica has room; otherwise, or when another
        driver holds it, the run is left to a sweep."""
        if run_id in self._runs or self._busy() >= self._concurrency:
            return None
        return self._track(run_id, self._claim_and_drive(run_id))

    async def sweep(self) -> list[str]:
        """Purge the runs past their retention, then claim and start driving the running runs no live driver holds, up
        to this replica's free room."""
        try:
            await self._store.purge(self._retention_days)
        except Exception:
            logger.warning("the run retention could not purge; it tries again at the next sweep", exc_info=True)
        free = self._concurrency - self._busy()
        if free <= 0:
            return []
        self._reserved += free
        try:
            # Never a run this process drives itself: a lease of its own that is about to be renewed is not a lapsed one.
            asked = asyncio.get_running_loop().time()
            claimed = await self._store.claim(self.owner, self._lease_seconds, limit=free, exclude=sorted(self._runs))
        finally:
            self._reserved -= free
        for run_id in claimed:
            if run_id not in self._runs:
                self._track(run_id, self._drive(run_id, asked))
        return claimed

    def _busy(self) -> int:
        return len(self._runs) + self._reserved

    def _track(self, run_id: str, run: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(run)
        self._runs[run_id] = task
        task.add_done_callback(lambda _done: self._runs.pop(run_id, None))
        return task

    async def _redrive(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("the run re-drive could not sweep; it tries again at its next tick")
            await asyncio.sleep(self._redrive_seconds * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, not secrecy

    async def _claim_and_drive(self, run_id: str) -> None:
        asked = asyncio.get_running_loop().time()
        if not await self._store.claim(self.owner, self._lease_seconds, run_id=run_id):
            logger.debug("run %s is driven by another process; this one stands down", run_id)
            return
        await self._drive(run_id, asked)

    async def _drive(self, run_id: str, claim_asked: float) -> None:
        """Drive one claimed run until it ends or stops being this driver's, then give its lease up.

        The lease is renewed every third of it, each renewal on its own: one the database refuses is tried again at the
        next tick. When the lease is gone (another driver holds the run, or it was cancelled) or no renewal got through
        and the next chance would come after the lease lapses, the step in flight is cancelled — its call to Serve with
        it — and nothing is recorded, so this driver never runs a step beside the one that resumes the run.
        """
        loop = asyncio.get_running_loop()
        work = loop.create_task(
            drive_run(self._store, run_id, self.owner, client=self._client, lease_seconds=self._lease_seconds, max_attempts=self._max_attempts)
        )
        lost = False

        async def heartbeat() -> None:
            nonlocal lost
            tick = self._lease_seconds / 3
            # The lease runs from when the claim that took it was ASKED (the database stamps it no earlier), and each
            # renewal is asked a tick after the one before was asked, not after it answered: a database that answers
            # slowly but inside the lease keeps a healthy driver its run.
            confirmed = next_ask = claim_asked
            while not work.done():
                next_ask += tick
                await asyncio.sleep(max(0.0, next_ask - loop.time()))
                asked = next_ask = loop.time()
                # Every renewal, its wait for a pooled connection included, is bounded by what is left of the lease
                # (less half a tick, so the step stops before the lease can lapse): a database that stops answering
                # must not keep this driver running a step another replica may claim.
                left = confirmed + self._lease_seconds - tick / 2 - asked
                try:
                    async with asyncio.timeout(left):
                        held = await self._store.renew(run_id, self.owner, self._lease_seconds)
                except TimeoutError:
                    logger.warning("renewing the lease on run %s did not answer before it would lapse", run_id)
                    held = False
                except Exception:
                    logger.warning("could not renew the lease on run %s; trying again", run_id, exc_info=True)
                    if loop.time() - confirmed + tick < self._lease_seconds - tick / 2:
                        continue
                    held = False
                if not held:
                    lost = True
                    work.cancel()
                    return
                confirmed = asked

        beating = loop.create_task(heartbeat())
        try:
            try:
                ended = await work
            except asyncio.CancelledError:
                if not lost:
                    raise
                logger.warning("run %s: the lease is no longer this driver's; its step was stopped and nothing recorded", run_id)
            else:
                logger.info("run %s ended: %s", run_id, ended)
        except* LostLease:
            logger.info("run %s is no longer this driver's (another driver holds it, or it was cancelled); this one stops", run_id)
        except* Exception:
            # A database the driver cannot reach: the run keeps its recorded steps, and a re-drive resumes it.
            logger.exception("driving run %s failed; a re-drive resumes it from its recorded steps", run_id)
        finally:
            beating.cancel()
            work.cancel()
            # When the lease is gone, or the database did not answer in time, a release would be refused or would wait
            # on the same database: the run is left to lapse at its expiry and stops counting as driven here now.
            if not lost:
                try:
                    # Stopped by this process's own shutdown (a rolling deploy), the run was not at fault: the attempt is
                    # given back, so a long run is never failed by its deploys. Bounded: a release that cannot reach the
                    # database leaves the lease to lapse at its expiry, and must not keep the run counted as driven here.
                    async with asyncio.timeout(self._lease_seconds):
                        await self._store.release(run_id, self.owner, give_back_attempt=self._closing)
                except Exception:
                    logger.warning("could not release the lease on run %s; it lapses at its expiry", run_id, exc_info=True)
