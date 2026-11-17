"""The flows service's run store: each run and each step's outcome, as rows in its own Postgres.

A run is durable because its state is rows, not a process: the run row holds what it executes (`RunJob`) and the
driver's lease, and a step's outcome is a row written in the transaction that checks the driver still holds the run.
A step with a row is never executed again, so a run re-driven after its driver died picks up at the first step with no
outcome (`flows.runner`).

**A driver holds a lease on its run's row, never a connection** (`claim`, `renew`, `release`): every call takes a pooled
connection for one transaction and gives it back, so no connection is held across a call to Serve. Every expiry is read
against the database's clock, the one clock every replica shares.

**One transaction moves a run on.** `record` locks the run row, refuses when the run is no longer this driver's or no
longer running, writes the step rows, renews the lease, and ends the run when its last step is recorded. Two drivers can
therefore never both record one step, and a run ends exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from importlib import resources
from string import Template
from typing import TYPE_CHECKING, Any, Final, cast, get_args

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from flows.models import NodeResult, NodeRunState, NodeStatus, RunJob, RunState, RunStatus


if TYPE_CHECKING:
    from flows.config import FlowsSettings


logger = logging.getLogger(__name__)

type Connection = AsyncConnection[tuple[Any, ...]]

#: The advisory lock every start takes while it applies the schema, so two replicas starting together apply it one
#: after the other. Any constant no other lock in this database uses.
SCHEMA_LOCK: Final = 0x666C6F7773


class LostLease(Exception):
    """The run is no longer this driver's to move on: another driver holds it, or it ended (finished or cancelled)."""


class StoredRun(BaseModel):
    """A run as its rows hold it: what it executes, where it stands, and the outcome of each step recorded so far."""

    run_id: str
    project: str
    subject: str
    state: RunStatus
    job: RunJob
    error: str | None = None
    #: How many times a driver claimed the run.
    attempts: int
    #: The digest of the request that started it (`models.RunRequest.digest`), which a replay is judged against.
    request_digest: str
    steps: dict[str, NodeResult]

    def as_state(self) -> RunState:
        return RunState(
            run_id=self.run_id,
            project=self.project,
            status=self.state,
            nodes={node_id: step.state for node_id, step in self.steps.items()},
            error=self.error,
        )


def connection_kwargs(settings: FlowsSettings) -> dict[str, Any]:
    """The libpq parameters of one new connection, the password read from its mounted file at that moment."""
    kwargs: dict[str, Any] = {
        "host": settings.state_host,
        "port": settings.state_port,
        "dbname": settings.state_database,
        "user": settings.state_user,
        "sslmode": settings.state_sslmode,
        "application_name": "flows",
        "connect_timeout": 10,
        "options": "-c TimeZone=UTC",
    }
    if settings.state_ca_file is not None:
        kwargs["sslrootcert"] = str(settings.state_ca_file)
    if settings.state_password_file is not None:
        kwargs["password"] = settings.state_password_file.read_text().rstrip("\n")
    return kwargs


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


async def apply_schema(pool: AsyncConnectionPool[Any]) -> None:
    """Apply `schema.sql`, every statement of which is safe to re-run, in one transaction under `SCHEMA_LOCK`."""
    template = Template(resources.files("flows").joinpath("schema.sql").read_text())
    schema = template.substitute(run_states=_sql_list(get_args(RunStatus)), step_states=_sql_list(get_args(NodeStatus)))
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        await conn.execute(schema.encode())


class FlowsStore:
    """The run store over one connection pool. Built and closed by the service's lifespan (`flows.lifespan`)."""

    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, settings: FlowsSettings) -> FlowsStore:
        """Connect, wait for the first connection, and apply the schema. Raises when the database cannot be reached."""
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            kwargs=lambda: connection_kwargs(settings),
            min_size=2,
            max_size=settings.state_pool_size,
            open=False,
            check=AsyncConnectionPool.check_connection,
            name="flows-state",
        )
        await pool.open(wait=True, timeout=30)
        try:
            await apply_schema(pool)
        except BaseException:
            await pool.close()
            raise
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[Connection]:
        async with self._pool.connection() as conn, conn.transaction():
            yield cast("Connection", conn)

    # ---------------------------------------------------------------------------------------------
    # Runs
    # ---------------------------------------------------------------------------------------------

    async def create(self, run_id: str, *, subject: str, job: RunJob) -> tuple[StoredRun, bool]:
        """Record a new run, unowned, for a driver to claim; answer it and whether it was new.

        The run id is derived from the caller's key (`routes.run_id_for`), so a retried or concurrent start with that key
        finds the row the first one wrote and starts nothing.
        """
        async with self._transaction() as conn:
            created = await (
                await conn.execute(
                    "INSERT INTO runs (run_id, project, subject, state, node_count, job, request_digest, created_at) "
                    "VALUES (%s, %s, %s, 'running', %s, %s, %s, clock_timestamp()) ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                    (run_id, job.project, subject, len(job.graph.nodes), Jsonb(job.model_dump(mode="json")), job.digest()),
                )
            ).fetchone()
            run = await _read(conn, run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} was written and cannot be read back")
        return run, created is not None

    async def get(self, run_id: str) -> StoredRun | None:
        async with self._transaction() as conn:
            return await _read(conn, run_id)

    async def cancel(self, run_id: str, project: str) -> StoredRun | None:
        """End ``project``'s running run as `cancelled` and drop its lease: its driver finds it ended at its next record
        and stops. A run that already ended keeps its outcome. Answers the run, or None when ``project`` has no such run."""
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE runs SET state = 'cancelled', finished_at = clock_timestamp(), owner = NULL, lease_until = NULL "
                "WHERE run_id = %s AND project = %s AND state = 'running'",
                (run_id, project),
            )
            run = await _read(conn, run_id)
        return run if run is not None and run.project == project else None

    # ---------------------------------------------------------------------------------------------
    # The driver's lease
    # ---------------------------------------------------------------------------------------------

    async def claim(self, owner: str, lease_seconds: float, *, run_id: str | None = None, limit: int = 1, exclude: list[str] | None = None) -> list[str]:
        """Claim the lease of running runs no live driver holds, for ``owner``; answer the ids claimed.

        Named, it claims that one run or none; otherwise up to ``limit``, skipping rows another claim holds this moment, so
        replicas sweeping together never claim one run twice. The run that waited longest since it was created or last
        claimed goes first, so a run re-driven again and again cannot starve runs that never ran. Every claim counts one
        attempt on the run. ``exclude`` names runs the caller drives itself, which it never claims again."""
        if limit < 1:
            return []
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "UPDATE runs SET owner = %s, lease_until = clock_timestamp() + make_interval(secs => %s), "
                    "  attempts = attempts + 1, last_claimed_at = clock_timestamp() "
                    "WHERE run_id IN ("
                    "  SELECT run_id FROM runs"
                    "  WHERE state = 'running' AND (lease_until IS NULL OR lease_until <= clock_timestamp())"
                    "    AND (%s::text IS NULL OR run_id = %s) AND run_id <> ALL(%s)"
                    "  ORDER BY coalesce(last_claimed_at, created_at), run_id LIMIT %s FOR UPDATE SKIP LOCKED"
                    ") RETURNING run_id",
                    (owner, lease_seconds, run_id, run_id, exclude or [], limit),
                )
            ).fetchall()
            return sorted(str(row[0]) for row in rows)

    async def renew(self, run_id: str, owner: str, lease_seconds: float) -> bool:
        """Extend ``owner``'s lease; `False` when it holds none (it lapsed and another driver claimed it, or the run ended)."""
        async with self._transaction() as conn:
            row = await (
                await conn.execute(
                    "UPDATE runs SET lease_until = clock_timestamp() + make_interval(secs => %s) "
                    "WHERE run_id = %s AND state = 'running' AND owner = %s RETURNING run_id",
                    (lease_seconds, run_id, owner),
                )
            ).fetchone()
            return row is not None

    async def release(self, run_id: str, owner: str, *, give_back_attempt: bool = False) -> None:
        """Give up ``owner``'s lease, if it still holds it, so another driver may claim the run at once; with
        ``give_back_attempt``, the claim that took it is not counted (the driver stopped for its own shutdown)."""
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE runs SET owner = NULL, lease_until = NULL, attempts = attempts - CASE WHEN %s THEN 1 ELSE 0 END WHERE run_id = %s AND owner = %s",
                (give_back_attempt, run_id, owner),
            )

    # ---------------------------------------------------------------------------------------------
    # Moving a run on
    # ---------------------------------------------------------------------------------------------

    async def record(self, run_id: str, owner: str, lease_seconds: float, steps: dict[str, NodeResult]) -> RunStatus:
        """Record ``steps``' outcomes and answer where the run stands after them, in one transaction.

        Refused with `LostLease`, writing nothing, when the run is no longer ``owner``'s or no longer running. A step that
        already has an outcome keeps it. When the run's last step is recorded, the run ends here: `failed` when any step
        failed, else `succeeded`.
        """
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT state, owner, node_count FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))).fetchone()
            if row is None or row[0] != "running" or row[1] != owner:
                raise LostLease(f"run {run_id} is no longer driven by {owner}")
            node_count = int(row[2])
            if steps:
                try:
                    await _insert_steps(conn, run_id, steps)
                except _REFUSED_AS_DATA as exc:
                    # The store refusing the DATA (a value it cannot hold, a key past an index's limit, a constraint) is
                    # deterministic: writing it again refuses it again. So it is the step's outcome, recorded once with
                    # the database's own reason, and when even that cannot be stored the run ends failed with it.
                    reason = f"the run store refused this step's outcome: {_refusal(exc)}"
                    try:
                        await _insert_steps(conn, run_id, {node_id: NodeResult(state=NodeRunState(status="failed", error=reason)) for node_id in steps})
                    except _REFUSED_AS_DATA as again:
                        await conn.execute(
                            "UPDATE runs SET state = 'failed', error = %s, finished_at = clock_timestamp(), owner = NULL, lease_until = NULL WHERE run_id = %s",
                            (f"the run store refused a step of this run: {_refusal(again)}", run_id),
                        )
                        return "failed"
            recorded, failed = cast(
                "tuple[int, bool]",
                await (
                    await conn.execute("SELECT count(*), coalesce(bool_or(state = 'failed'), false) FROM run_steps WHERE run_id = %s", (run_id,))
                ).fetchone(),
            )
            if recorded < node_count:
                await conn.execute(
                    "UPDATE runs SET lease_until = clock_timestamp() + make_interval(secs => %s) WHERE run_id = %s",
                    (lease_seconds, run_id),
                )
                return "running"
            ended: RunStatus = "failed" if failed else "succeeded"
            await conn.execute(
                "UPDATE runs SET state = %s, finished_at = clock_timestamp(), owner = NULL, lease_until = NULL WHERE run_id = %s",
                (ended, run_id),
            )
            return ended

    async def purge(self, finished_before_days: float, *, limit: int = 500) -> int:
        """Delete up to ``limit`` runs that ended more than ``finished_before_days`` ago, with their steps; answer how many.

        Safe in every replica at once: a run is deleted once whoever gets to it first, and a row another purge holds this
        moment is skipped, not waited for."""
        async with self._transaction() as conn:
            deleted = await conn.execute(
                "DELETE FROM runs WHERE run_id IN ("
                "  SELECT run_id FROM runs WHERE finished_at < clock_timestamp() - make_interval(secs => %s)"
                "  ORDER BY finished_at LIMIT %s FOR UPDATE SKIP LOCKED"
                ")",
                (finished_before_days * 86400, limit),
            )
            return deleted.rowcount

    async def fail(self, run_id: str, owner: str, error: str) -> None:
        """End ``owner``'s run as failed for a reason no step names: its graph cannot be ordered, or it was claimed past
        its attempt bound."""
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE runs SET state = 'failed', error = %s, finished_at = clock_timestamp(), owner = NULL, lease_until = NULL "
                "WHERE run_id = %s AND state = 'running' AND owner = %s",
                (error, run_id, owner),
            )


#: What the store answers when it refuses the data itself, not the connection: a value it cannot hold
#: (`DataError`), a value past a limit such as an index row's (`ProgramLimitExceeded`), a violated constraint
#: (`IntegrityError`; the step key's own conflict is `ON CONFLICT DO NOTHING`, never raised).
_REFUSED_AS_DATA = (psycopg.errors.DataError, psycopg.errors.ProgramLimitExceeded, psycopg.errors.IntegrityError)


def _refusal(exc: psycopg.Error) -> str:
    """The database's own short reason, bounded, never the value it refused."""
    message = exc.diag.message_primary or type(exc).__name__
    return f"{type(exc).__name__}: {message[:300]}"


async def _insert_steps(conn: Connection, run_id: str, steps: dict[str, NodeResult]) -> None:
    """Insert the steps in a savepoint of their own, so a refusal leaves the transaction usable."""
    async with conn.transaction(), conn.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO run_steps (run_id, node_id, state, outcome, payload, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, clock_timestamp()) ON CONFLICT (run_id, node_id) DO NOTHING",
            [(run_id, node_id, result.state.status, Jsonb(result.state.model_dump(mode="json")), result.payload_text) for node_id, result in steps.items()],
        )


async def _read(conn: Connection, run_id: str) -> StoredRun | None:
    row = await (await conn.execute("SELECT project, subject, state, job, error, attempts, request_digest FROM runs WHERE run_id = %s", (run_id,))).fetchone()
    if row is None:
        return None
    steps = await (await conn.execute("SELECT node_id, outcome, payload FROM run_steps WHERE run_id = %s ORDER BY node_id", (run_id,))).fetchall()
    return StoredRun(
        run_id=run_id,
        project=row[0],
        subject=row[1],
        state=row[2],
        job=RunJob.model_validate(row[3]),
        error=row[4],
        attempts=row[5],
        request_digest=row[6],
        steps={str(node_id): NodeResult(state=NodeRunState.model_validate(outcome), payload_text=payload) for node_id, outcome, payload in steps},
    )
