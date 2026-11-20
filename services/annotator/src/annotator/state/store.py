"""The annotator's state store: its projects, tasks, drafts and claims, as rows in its own Postgres.

Workflow state with claims and leases is transactional row state, so it lives in a relational database the annotator
owns (the chart's CloudNativePG `Cluster`), reached directly with psycopg. Work in progress is kept here too: each
task's draft is the one store of the labels people make (the canvas reads and saves it through `annotator.annotations`,
each save kept in a bounded history), and nothing reaches the lakehouse until a publish writes the project's accepted
work through the catalog's doors (`annotator.projects.lakehouse`).

**A transition is one transaction.** It locks the project row, then the task row (`SELECT … FOR UPDATE`), applies the
pure machine (`annotator.projects.machines`), writes and commits. The row lock is what serialises two people acting
on one task: of two concurrent claims exactly one finds the task unassigned. Every rule that depends on the task's
own rows (who holds the claim, who submitted, whether the project is frozen) is checked inside that transaction, on
rows that cannot move under the check; the HTTP layer's check of the same rules on its own read is what gives a 403
its reason, and is never what decides.

**One lock order, so no two transactions deadlock on each other's rows:** a project before its tasks, and several rows
of one table always in key order — a consensus group's replicas and a send's tasks and groups by id. A task transition
takes its project `FOR SHARE`, so it cannot interleave with a project write (`FOR NO KEY UPDATE`): a task cannot move
while a publish checks that every task is terminal, and none moves once the project is frozen with a publish.

**A publish run holds a lease on its project's row, never a connection** (`claim_publish`, `renew_publish`,
`release_publish`): every store call takes a pooled connection for one transaction and gives it back, so no connection
is held across a call to the catalog.

**What a query answers is not stored:** a project's counts, its task index and whether it may publish are read from
the task rows in the same transaction (`task_states`).

**A lease has no timer.** A claim is its holder and an expiry; one past its expiry is unassigned to every reader
(`task_states`, `machines.lapse_expired_claim`, both against the database's clock), and its `lease_expired` edge is
written the next time the row is locked.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from importlib import resources
from string import Template
from typing import TYPE_CHECKING, Any, Final, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from annotator.projects.machines import (
    EDIT_PERMISSION,
    FROZEN_PROJECT_STATES,
    PROJECT_SEND_STATES,
    SETTLES_PROPOSALS,
    IllegalTransition,
    edit_violation,
    identity_violation,
    lapse_expired_claim,
    project_transition,
    refuse_unreadable_ontology,
    submit_target,
    task_transition,
    unsettled_proposals,
)
from annotator.projects.models import (
    PROPOSED,
    TERMINAL_TASK_STATES,
    Adjudication,
    AnnotationProject,
    Draft,
    DraftOrigin,
    Link,
    ProjectSettings,
    ProjectState,
    PublishRecord,
    ReviewNote,
    Shape,
    Task,
    TaskState,
    Transition,
    labels_of,
    new_id,
)
from annotator.projects.ontology import LabelOntology, LinkLike, ShapeLike, membership_violation, validate_against_ontology
from annotator.state.task_query import Contributor, SortOrder, TaskFilter, TaskPage, TaskSort, contributors_of, page_of, work_of
from annotator.text import storable_json, storable_text
from service_kit.exceptions import ConflictError, DomainError


if TYPE_CHECKING:
    from annotator.core.config import AnnotatorSettings


logger = logging.getLogger(__name__)

type Connection = AsyncConnection[tuple[Any, ...]]

#: The advisory lock every start takes while it applies the schema, so two replicas starting together apply it one
#: after the other. Any constant no other lock in this database uses.
SCHEMA_LOCK: Final = 0x616E6E6F74617465

#: The project states a task may still be dropped in. Past `frozen` the answer set is closed and a publish is being
#: prepared against it, so removing an item would change what the run facet describes after the description was fixed.
DROPPABLE_STATES: Final = frozenset({ProjectState.DRAFT, ProjectState.LABELING})


#: The most shapes, links and serialized bytes one task's draft holds. The draft is one document, rewritten whole on
#: every save, so these bound what a save writes and what a read serves. Ten thousand shapes is a densely word-boxed
#: page (measured: a box serializes to about 470 bytes, so 10,000 of them are about 4.7 MB); the bytes cap, 8 MiB, leaves
#: such a page room for its texts and holds a draft of fewer shapes that carry long texts or polygons. An item that needs more is the "one row per shape for
#: very large items" the owner deferred until it is needed.
MAX_DRAFT_SHAPES: Final = 10_000
MAX_DRAFT_LINKS: Final = 10_000
MAX_DRAFT_BYTES: Final = 8 * 1024 * 1024

#: The saves of one task the history keeps: at most this many, and at most `MAX_HISTORY_BYTES` of them. Fifty covers a
#: working session of the canvas's autosaves (one after each pause in the work) and the twenty the history panel lists
#: at first.
MAX_DRAFT_HISTORY: Final = 50

#: The most bytes of changes one task's history keeps. A history row holds its save's REVERSE change (the shapes as they
#: were before it, for every shape it changed or removed, the ids it added, and the links before it when they changed),
#: not a copy of the draft, and a past save is rebuilt from the draft by undoing the saves after it. An autosave's change
#: is a few rows, about a kilobyte, so fifty of them cost about 50 KB; the bound holds the rare saves that replace a large
#: part of the work. A task's worst case is then one capped draft and this much history: 8 + 8 = 16 MiB, where fifty
#: whole copies would have been 50 × 8 = 400 MiB.
MAX_HISTORY_BYTES: Final = 8 * 1024 * 1024


class DraftTooLarge(DomainError):
    """A change that would take a task's draft past one of its caps."""

    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    title = "Content Too Large"
    problem_type = "about:blank#draft-too-large"


#: A change of a draft: given the draft the transaction read (None before the first save) and the database's clock,
#: the shapes and links it holds next. It may raise to refuse, and then nothing is written.
type DraftChange = Callable[[Draft | None, datetime], tuple[list[Shape], list[Link]]]


class DraftRevision(BaseModel):
    """One kept save of a task's draft: its revision, when and by whom it was saved, and how many shapes it held."""

    revision: int
    saved_at: datetime
    author: str
    shapes: int


class ReverseChange(BaseModel):
    """What undoes one save: the shapes as they were before it (each one it changed or removed), the ids of the shapes it
    added, and the links before it when it changed them."""

    restore: list[Shape] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    links: list[Link] | None = None

    @classmethod
    def of(cls, before: Draft | None, shapes: list[Shape], links: list[Link]) -> ReverseChange:
        prior = {s.shape_id: s for s in (before.shapes if before else [])}
        after = {s.shape_id: s for s in shapes}
        prior_links = before.links if before else []
        return cls(
            restore=[s for sid, s in prior.items() if after.get(sid) != s],
            remove=[sid for sid in after if sid not in prior],
            links=prior_links if prior_links != links else None,
        )

    def undo(self, shapes: list[Shape], links: list[Link]) -> tuple[list[Shape], list[Link]]:
        """The shapes and links before the save, from those after it. A shape the save removed comes back after the others."""
        gone = set(self.remove)
        kept = {s.shape_id: s for s in shapes if s.shape_id not in gone}
        for shape in self.restore:
            kept[shape.shape_id] = shape
        return list(kept.values()), list(links if self.links is None else self.links)


def _stored(kind: str, document: Any) -> Jsonb:
    """``document`` as the store writes it: THE write boundary every jsonb value passes. Its text is serialised and
    checked once (`annotator.text.storable_json`), and that checked text is what is sent, so nothing can come between
    the check and the write; ``kind`` names the document in a refusal."""
    text = storable_json(kind, document)
    return Jsonb(document, dumps=lambda _document: text)


def _stored_draft(draft: Draft) -> Jsonb:
    """The draft as it is written, once it fits its caps: its size is the size of the text written."""
    if len(draft.shapes) > MAX_DRAFT_SHAPES:
        raise DraftTooLarge(f"this change leaves the task's work at {len(draft.shapes)} shapes, past its cap of {MAX_DRAFT_SHAPES} shapes")
    if len(draft.links) > MAX_DRAFT_LINKS:
        raise DraftTooLarge(f"this change leaves the task's work at {len(draft.links)} links, past its cap of {MAX_DRAFT_LINKS} links")
    text = storable_json("the task's work", draft.model_dump(mode="json"))
    size = len(text.encode())
    if size > MAX_DRAFT_BYTES:
        raise DraftTooLarge(f"this change leaves the task's work at {size} bytes, past its cap of {MAX_DRAFT_BYTES} bytes")
    return Jsonb(None, dumps=lambda _document: text)


class TaskIndex(BaseModel):
    """How many of a project's tasks are in each state, and the publish precondition, read in one statement."""

    counts: dict[TaskState, int]
    total: int
    terminal: int
    may_publish: bool


class DropReceipt(BaseModel):
    task_id: str
    removed: bool
    total: int


def connection_kwargs(settings: AnnotatorSettings) -> dict[str, Any]:
    """The libpq parameters of one new connection, the password read from its mounted file at that moment."""
    kwargs: dict[str, Any] = {
        "host": settings.state_host,
        "port": settings.state_port,
        "dbname": settings.state_database,
        "user": settings.state_user,
        "sslmode": settings.state_sslmode,
        "application_name": "annotator",
        "connect_timeout": 10,
        # Every stamp the store reads back from the database's clock is in UTC, whatever the server's own zone.
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
    """Apply `schema.sql`, every statement of which is safe to re-run, in one transaction under `SCHEMA_LOCK`, its state
    checks filled from the enums the models use."""
    template = Template(resources.files("annotator.state").joinpath("schema.sql").read_text())
    schema = template.substitute(project_states=_sql_list(ProjectState), task_states=_sql_list(TaskState))
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        await conn.execute(schema.encode())


def _project(document: dict[str, Any]) -> AnnotationProject:
    try:
        return AnnotationProject.model_validate(document)
    except PydanticValidationError as exc:
        refuse_unreadable_ontology(exc, "project", str(document.get("project_id") or "unknown"))


def _task(document: dict[str, Any]) -> Task:
    try:
        return Task.model_validate(document)
    except PydanticValidationError as exc:
        refuse_unreadable_ontology(exc, "task", str(document.get("task_id") or "unknown"))


def _refuse_if_frozen(project: AnnotationProject, task: Task, event: str) -> None:
    """§5.2 rule 5: nothing escapes a published project, read off the project row this transaction holds."""
    if project.state in FROZEN_PROJECT_STATES:
        raise IllegalTransition(
            "task",
            task.state,
            event,
            message=f"this project is {project.state} — {event} is rejected; provenance is frozen with the published artifact",
        )


class AnnotatorStore:
    """The state store over one connection pool. Built and closed by the service's lifespan (`annotator.main`)."""

    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, settings: AnnotatorSettings) -> AnnotatorStore:
        """Connect, wait for the first connection, and apply the schema. Raises when the database cannot be reached."""
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            kwargs=lambda: connection_kwargs(settings),
            # Two from the start, so two requests arriving together run side by side from the first moment.
            min_size=2,
            max_size=settings.state_pool_size,
            open=False,
            check=AsyncConnectionPool.check_connection,
            name="annotator-state",
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
    # Projects
    # ---------------------------------------------------------------------------------------------

    async def _lock_project(self, conn: Connection, project_id: str, *, write: bool) -> AnnotationProject:
        query = (
            "SELECT document FROM projects WHERE project_id = %s FOR NO KEY UPDATE"
            if write
            else "SELECT document FROM projects WHERE project_id = %s FOR SHARE"
        )
        row = await (await conn.execute(query, (project_id,))).fetchone()
        if row is None:
            raise IllegalTransition("project", "absent", "any", message=f"annotation project {project_id} does not exist")
        return _project(row[0])

    @staticmethod
    async def _write_project(conn: Connection, project: AnnotationProject) -> None:
        now = await _clock(conn)
        project.updated_at = now
        # Counts are read, never stored: a stored count and the rows it counts are two truths.
        document = project.model_dump(mode="json", exclude={"counts"})
        # A publish lease means something only while the project is publishing: leaving that state ends it, so a retried
        # publish is claimable at once whatever the previous run is still doing on its way out.
        await conn.execute(
            "UPDATE projects SET state = %s, document = %s, "
            "publish_owner = CASE WHEN %s = 'publishing' THEN publish_owner END, "
            "publish_lease_until = CASE WHEN %s = 'publishing' THEN publish_lease_until END "
            "WHERE project_id = %s",
            (str(project.state), _stored("the project", document), str(project.state), str(project.state), project.project_id),
        )

    @staticmethod
    async def _with_counts(conn: Connection, projects: list[AnnotationProject]) -> list[AnnotationProject]:
        ids = [p.project_id for p in projects]
        rows = await (await conn.execute("SELECT project_id, state, count(*) FROM task_states WHERE project_id = ANY(%s) GROUP BY 1, 2", (ids,))).fetchall()
        for project in projects:
            project.counts = dict.fromkeys(TaskState, 0)
        by_id = {p.project_id: p for p in projects}
        for project_id, state, count in rows:
            by_id[project_id].counts[TaskState(state)] = int(count)
        return projects

    async def create_project(self, project: AnnotationProject) -> AnnotationProject:
        """Store a new project, stamped with its creation on the database's clock (the column and the document hold the
        one value). Its id is minted per create, so a second create is a second project."""
        async with self._transaction() as conn:
            project.created_at = project.updated_at = await _clock(conn)
            document = project.model_dump(mode="json", exclude={"counts"})
            await conn.execute(
                "INSERT INTO projects (project_id, tenant, state, created_at, document) VALUES (%s, %s, %s, %s, %s)",
                (project.project_id, project.tenant, str(project.state), project.created_at, _stored("the project", document)),
            )
            return (await self._with_counts(conn, [project]))[0]

    async def get_project(self, project_id: str) -> AnnotationProject | None:
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT document FROM projects WHERE project_id = %s", (project_id,))).fetchone()
            if row is None:
                return None
            return (await self._with_counts(conn, [_project(row[0])]))[0]

    async def list_projects(self, tenant: str) -> list[AnnotationProject]:
        """An organisation's projects, oldest first."""
        async with self._transaction() as conn:
            rows = await (await conn.execute("SELECT document FROM projects WHERE tenant = %s ORDER BY created_at, project_id", (tenant,))).fetchall()
            return await self._with_counts(conn, [_project(row[0]) for row in rows])

    # ---------------------------------------------------------------------------------------------
    # The publish lease
    # ---------------------------------------------------------------------------------------------

    async def claim_publish(self, owner: str, lease_seconds: int, *, project_id: str | None = None, limit: int = 1) -> list[str]:
        """Claim the publish lease of publishing projects no live run holds, for ``owner``, until ``lease_seconds`` past
        the database's clock; answer the ids claimed. Named, it claims that one project or none; otherwise up to ``limit``,
        skipping rows another claim holds this moment, so replicas sweeping together never claim one project twice."""
        if limit < 1:
            return []
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "UPDATE projects SET publish_owner = %s, publish_lease_until = clock_timestamp() + make_interval(secs => %s) "
                    "WHERE project_id IN ("
                    "  SELECT project_id FROM projects"
                    "  WHERE state = 'publishing' AND (publish_lease_until IS NULL OR publish_lease_until <= clock_timestamp())"
                    "    AND (%s::text IS NULL OR project_id = %s)"
                    "  ORDER BY publish_lease_until NULLS FIRST, project_id LIMIT %s FOR UPDATE SKIP LOCKED"
                    ") RETURNING project_id",
                    (owner, lease_seconds, project_id, project_id, limit),
                )
            ).fetchall()
            return sorted(str(row[0]) for row in rows)

    async def renew_publish(self, project_id: str, owner: str, lease_seconds: int) -> bool:
        """Extend ``owner``'s lease on the project's publish; `False` when it holds none (it lapsed and another run claimed
        it, or the publish ended)."""
        async with self._transaction() as conn:
            row = await (
                await conn.execute(
                    "UPDATE projects SET publish_lease_until = clock_timestamp() + make_interval(secs => %s) "
                    "WHERE project_id = %s AND state = 'publishing' AND publish_owner = %s RETURNING project_id",
                    (lease_seconds, project_id, owner),
                )
            ).fetchone()
            return row is not None

    async def release_publish(self, project_id: str, owner: str) -> None:
        """Give up ``owner``'s lease, if it still holds it, so another run may claim the project at once."""
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE projects SET publish_owner = NULL, publish_lease_until = NULL WHERE project_id = %s AND publish_owner = %s",
                (project_id, owner),
            )

    async def _update_project(self, project_id: str, change: Callable[[Connection, AnnotationProject], Any]) -> AnnotationProject:
        """Lock the project for writing, apply ``change`` (which may refuse), write it, and answer it with its counts."""
        async with self._transaction() as conn:
            project = await self._lock_project(conn, project_id, write=True)
            await change(conn, project)
            await self._write_project(conn, project)
            return (await self._with_counts(conn, [project]))[0]

    async def fire_project(
        self, project_id: str, *, event: str, actor: str | None, target_namespace: str | None = None, error: str | None = None
    ) -> AnnotationProject:
        """Apply one edge of `PROJECT_EDGES`.

        `publish` carries the precondition the table cannot express: every task terminal (§5.1), read from the task rows
        while this transaction holds the project, so no task can move between the read and the transition. It mints the
        publish token and instant once and pins the target namespace with them: the published table's id derives from
        the token, so a retry finds the table it already created rather than making a second one, and a retry naming
        another namespace is refused.
        """

        async def change(conn: Connection, project: AnnotationProject) -> None:
            target, _permission = project_transition(project.state, event)
            if event == "publish":
                index = await _task_index(conn, project_id)
                if not index.total:
                    raise IllegalTransition("project", project.state, "publish (no tasks)")
                if not index.may_publish:
                    raise IllegalTransition("project", project.state, "publish (tasks are not all terminal)")
                requested = target_namespace or project.pending_target_namespace
                if not requested:
                    raise IllegalTransition("project", project.state, "publish (no target namespace)")
                if project.pending_publish_id is None:
                    project.pending_publish_id = new_id()
                    project.pending_publish_at = await _clock(conn)
                    project.pending_target_namespace = requested
                elif project.pending_target_namespace and requested != project.pending_target_namespace:
                    raise IllegalTransition(
                        "project",
                        project.state,
                        f"publish (pending publish targets {project.pending_target_namespace!r}, not {requested!r} — the retry must restate the pinned target)",
                    )
                # Re-stated per attempt: the record names the person whose action produced the publish that succeeded.
                project.pending_publish_by = actor or project.pending_publish_by
                project.publish_error = None
                project.publish_progress = None
            elif event == "publish_failed":
                # `publish_progress` is kept: it names the step that was running when the attempt died.
                project.publish_error = error or "publish failed"
            elif event == "publish_succeeded":
                project.publish_progress = None
            project.state = target

        return await self._update_project(project_id, change)

    async def set_ontology(self, project_id: str, ontology: LabelOntology) -> AnnotationProject:
        """Replace the ontology while the project still takes work. An item sent and not started yet takes it too; one
        someone has started keeps the copy it captured."""

        async def change(conn: Connection, project: AnnotationProject) -> None:
            if project.state not in PROJECT_SEND_STATES:
                raise IllegalTransition(
                    "project",
                    project.state,
                    "set_ontology",
                    message=f"this project is {project.state.value} — the ontology is editable only in {sorted(s.value for s in PROJECT_SEND_STATES)}",
                )
            project.ontology = ontology
            await _update_unstarted(conn, project_id, {"ontology": ontology.model_dump(mode="json")})

        return await self._update_project(project_id, change)

    async def update_settings(self, project_id: str, settings: ProjectSettings) -> AnnotationProject:
        """Change the settings ``settings`` names, each when the project still lets it change:

        - title, description and instructions until a publish starts (`FROZEN_PROJECT_STATES`);
        - whether review is required and how long a claim lasts while the project takes work (`PROJECT_SEND_STATES`),
          taken by every item nobody has started as the ontology is (`set_ontology`), and kept by the rest, which
          captured theirs at send;
        - the consensus only while the project holds no item: each sent item was made into that many replicas, so a
          changed count would leave groups with too many or too few.
        """
        named = settings.model_dump(exclude_unset=True, exclude_none=True)

        async def change(conn: Connection, project: AnnotationProject) -> None:
            if project.state in FROZEN_PROJECT_STATES:
                raise IllegalTransition(
                    "project",
                    project.state,
                    "update_settings",
                    message=f"this project is {project.state.value}: its settings are fixed once a publish starts",
                )
            captured = {key: named[key] for key in ("review_required", "lease_seconds") if key in named}
            if captured and project.state not in PROJECT_SEND_STATES:
                raise IllegalTransition(
                    "project",
                    project.state,
                    "update_settings",
                    message=f"this project is {project.state.value}: review and claim settings change only in {sorted(s.value for s in PROJECT_SEND_STATES)}",
                )
            if named.get("consensus_n", project.consensus_n) != project.consensus_n:
                # Items, not tasks: on a consensus project each sent item is its replicas, one group.
                held = await (
                    await conn.execute("SELECT count(DISTINCT coalesce(replica_of, task_id)) FROM tasks WHERE project_id = %s", (project_id,))
                ).fetchone()
                items = int(held[0]) if held else 0
                if items > 0:
                    replicas = "1 replica" if project.consensus_n == 1 else f"{project.consensus_n} replicas"
                    raise IllegalTransition(
                        "project",
                        project.state,
                        "update_settings",
                        message=f"this project holds {items} item{'' if items == 1 else 's'}, each sent as {replicas}: the consensus is fixed once items are sent",
                    )
            for key, value in named.items():
                setattr(project, key, value)
            if captured:
                await _update_unstarted(conn, project_id, captured)

        return await self._update_project(project_id, change)

    async def adjudicate(self, project_id: str, *, group: str, task_id: str | None, actor: str) -> AnnotationProject:
        """Record, or with no ``task_id`` clear, the manager's canonical pick for one replica group (a pick, never a blend).

        Refused once provenance is frozen with a publish. The target must be the group's own replica (`{group}-r{k}`, the
        exact sibling shape: a prefix check would admit `g1-r1-r2` as a member of `g1`), in this project and accepted.
        Clearing exists because a pick has no other exit: the publish refuses a stale pick, so a wrong one would wedge it.
        """

        async def change(conn: Connection, project: AnnotationProject) -> None:
            if project.state in FROZEN_PROJECT_STATES:
                raise IllegalTransition("project", project.state, "adjudicate (provenance is frozen with the published artifact)")
            if not task_id:
                project.adjudications.pop(group, None)
                return
            if not _is_replica_of(task_id, group):
                raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is not a replica of group {group})")
            members = await (await conn.execute("SELECT state FROM task_states WHERE project_id = %s AND task_id = %s", (project_id, task_id))).fetchone()
            if members is None:
                raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is not in this project)")
            if TaskState(members[0]) is not TaskState.ACCEPTED:
                raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is {members[0]}, not accepted — only accepted work can be canonical)")
            project.adjudications[group] = Adjudication(task_id=task_id, by=actor, at=await _clock(conn))

        return await self._update_project(project_id, change)

    async def record_publish(self, project_id: str, record: PublishRecord) -> PublishRecord:
        """Write the publish record, once (§4.1): a second call answers the first, so a resumed publish never records a
        second, contradictory provenance."""
        stored: list[PublishRecord] = []

        async def change(_conn: Connection, project: AnnotationProject) -> None:
            if project.published is None:
                project.published = record
            stored.append(project.published)

        await self._update_project(project_id, change)
        return stored[0]

    async def note_progress(self, project_id: str, step: str) -> None:
        """Record the publish's current step, only while it is publishing: a note from a run that has since finished
        must not scribble on its result."""

        async def change(_conn: Connection, project: AnnotationProject) -> None:
            if project.state is ProjectState.PUBLISHING:
                project.publish_progress = step

        await self._update_project(project_id, change)

    # ---------------------------------------------------------------------------------------------
    # Sending and dropping tasks
    # ---------------------------------------------------------------------------------------------

    async def send_tasks(self, project_id: str, tasks: Iterable[Task], *, cap: int | None = None) -> list[str]:
        """Add tasks to the project in one transaction, and answer the ids this call created.

        **What a task captures from its project is decided here, from the project row this transaction holds**, never
        from a copy the caller read earlier: its `review_required`, `lease_seconds` and ontology are stamped from that
        row, its predictions are judged against that ontology, and on a project with consensus N an item sent as one task
        (no `replica_of`) is made into its N replicas `{id}-r{k}` here. The row is held `FOR SHARE`, which conflicts with
        the `FOR NO KEY UPDATE` every settings and ontology change takes (`_update_project`), so none of them can commit
        between this read and this write: a send and a settings change are ordered, and the send's tasks carry the
        settings of the one it comes after. A task already a replica is written as it is (a caller that names its own
        groups). ``cap`` bounds the tasks one send makes, replicas included, counted against the consensus read here.

        Idempotent on the task id: one already in this project is left as it is (a re-send must not reset a claim
        somebody holds). One that belongs to ANOTHER project, or a replica of a consensus group another project holds,
        refuses the whole send and nothing is written.

        Rows are written in key order, groups then tasks, each sorted by id: two sends naming overlapping ids in
        different orders then wait on one another rather than deadlock.
        """
        async with self._transaction() as conn:
            project = await self._lock_project(conn, project_id, write=False)
            if project.state not in PROJECT_SEND_STATES:
                raise IllegalTransition("project", project.state, "send")
            ordered = sorted(_captured(project, list(tasks)), key=lambda t: t.task_id)
            if cap is not None and len(ordered) > cap:
                raise ConflictError(f"this send makes {len(ordered)} tasks (consensus_n={project.consensus_n}), over the {cap}-task send cap — split the send")
            now = await _clock(conn)
            for task in ordered:
                task.sent_at = now
                task.trim_history()
            # Every document passes the write boundary before anything is written: a group id is one of their values.
            rows = [_task_row(task) for task in ordered]
            for group in sorted({t.replica_of for t in ordered if t.replica_of}):
                await conn.execute(
                    "INSERT INTO replica_groups (replica_of, project_id) VALUES (%s, %s) ON CONFLICT (replica_of) DO NOTHING", (group, project_id)
                )
                holder = await (await conn.execute("SELECT project_id FROM replica_groups WHERE replica_of = %s", (group,))).fetchone()
                if holder is not None and holder[0] != project_id:
                    raise ConflictError(
                        f"the consensus group {group} this send names already belongs to another project — its replicas are not sent here",
                        extensions={"group": group},
                    )
            created: list[str] = []
            for task, values in zip(ordered, rows, strict=True):
                row = await (
                    await conn.execute(
                        "INSERT INTO tasks (task_id, project_id, state, assignee, lease_expires_at, replica_of, document) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (task_id) DO NOTHING RETURNING task_id",
                        values,
                    )
                ).fetchone()
                if row is not None:
                    created.append(task.task_id)
                    continue
                owner = await (await conn.execute("SELECT project_id FROM tasks WHERE task_id = %s", (task.task_id,))).fetchone()
                if owner is not None and owner[0] != project_id:
                    raise ConflictError(
                        f"the item {task.task_id} this send names already belongs to another project — it is not sent here",
                        extensions={"task": task.task_id},
                    )
            return created

    async def drop_task(self, project_id: str, task_id: str) -> DropReceipt:
        """Remove one item, its draft and any canonical pick naming it. Idempotent: an absent task is `removed: false`."""
        async with self._transaction() as conn:
            project = await self._lock_project(conn, project_id, write=True)
            if project.state not in DROPPABLE_STATES:
                raise IllegalTransition(
                    "project",
                    project.state,
                    "drop_task",
                    message=f"this project is {project.state.value} — items can be dropped only in {sorted(s.value for s in DROPPABLE_STATES)}",
                )
            gone = await (await conn.execute("DELETE FROM tasks WHERE project_id = %s AND task_id = %s RETURNING task_id", (project_id, task_id))).fetchone()
            if gone is not None:
                project.adjudications = {group: pick for group, pick in project.adjudications.items() if pick.task_id != task_id}
                await self._write_project(conn, project)
            total = await (await conn.execute("SELECT count(*) FROM tasks WHERE project_id = %s", (project_id,))).fetchone()
            return DropReceipt(task_id=task_id, removed=gone is not None, total=int(total[0]) if total else 0)

    # ---------------------------------------------------------------------------------------------
    # Reads
    # ---------------------------------------------------------------------------------------------

    async def get_task(self, task_id: str) -> Task | None:
        """One task as every reader sees it: a lapsed claim reads as released."""
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT document, clock_timestamp() FROM tasks WHERE task_id = %s", (task_id,))).fetchone()
        if row is None:
            return None
        task = _task(row[0])
        lapse_expired_claim(task, row[1])
        return task

    async def get_project_task(self, project_id: str, task_id: str) -> Task | None:
        """One task of the project, or `None` for a task id the project does not hold, whether it names a task of another
        project or none at all: a door scoped to a project answers both alike."""
        async with self._transaction() as conn:
            row = await (
                await conn.execute("SELECT document, clock_timestamp() FROM tasks WHERE task_id = %s AND project_id = %s", (task_id, project_id))
            ).fetchone()
        if row is None:
            return None
        task = _task(row[0])
        lapse_expired_claim(task, row[1])
        return task

    async def get_tasks(self, project_id: str, task_ids: list[str]) -> list[Task]:
        """The project's tasks among ``task_ids``, in task-id order, lapsed claims released."""
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT document, clock_timestamp() FROM tasks WHERE project_id = %s AND task_id = ANY(%s) ORDER BY task_id",
                    (project_id, task_ids),
                )
            ).fetchall()
        tasks = []
        for document, now in rows:
            task = _task(document)
            lapse_expired_claim(task, now)
            tasks.append(task)
        return tasks

    async def get_draft(self, task_id: str) -> Draft | None:
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT document FROM drafts WHERE task_id = %s", (task_id,))).fetchone()
        return None if row is None else Draft.model_validate(row[0])

    async def task_index(self, project_id: str) -> TaskIndex:
        async with self._transaction() as conn:
            return await _task_index(conn, project_id)

    async def task_page(
        self,
        project_id: str,
        filters: TaskFilter,
        *,
        sort: TaskSort,
        order: SortOrder,
        limit: int,
        offset: int,
        around: str | None = None,
        contributors: bool = False,
        work: bool = False,
    ) -> tuple[TaskIndex, TaskPage, list[Task], list[Contributor] | None]:
        """The project's index, one filtered page of its items with their documents in the page's order, with ``work``
        each listed item's draft revision and tags (on the page, `TaskPage.work`), and with ``contributors`` each
        person's part, all from ONE snapshot (a repeatable-read transaction), so the page, its counts, its work and the
        publish precondition beside it never disagree."""
        async with self._pool.connection() as raw, raw.transaction():
            conn = cast("Connection", raw)
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            index = await _task_index(conn, project_id)
            page = await page_of(conn, project_id, filters, sort=sort, order=order, limit=limit, offset=offset, around=around)
            # The documents' lapsed claims are released on the snapshot's own clock (`now()`), the one the page's filters
            # and counts read a lapse by, so an item counted unassigned is answered unassigned.
            rows = await (
                await conn.execute("SELECT document, now() FROM tasks WHERE project_id = %s AND task_id = ANY(%s)", (project_id, page.task_ids))
            ).fetchall()
            if work:
                page.work = await work_of(conn, page.task_ids)
            people = await contributors_of(conn, project_id) if contributors else None
        by_id: dict[str, Task] = {}
        for document, now in rows:
            task = _task(document)
            lapse_expired_claim(task, now)
            by_id[task.task_id] = task
        return index, page, [by_id[tid] for tid in page.task_ids if tid in by_id], people

    async def remember_person(self, principal: str, name: str) -> None:
        """Keep the name ``principal``'s own verified token carried, replacing an older one; writes only on a change."""
        async with self._transaction() as conn:
            await conn.execute(
                "INSERT INTO people (principal, name, seen_at) VALUES (%s, %s, clock_timestamp()) "
                "ON CONFLICT (principal) DO UPDATE SET name = EXCLUDED.name, seen_at = EXCLUDED.seen_at WHERE people.name IS DISTINCT FROM EXCLUDED.name",
                (storable_text("a person's principal", principal), storable_text("a person's name", name)),
            )

    async def names_of(self, principals: Iterable[str]) -> dict[str, str]:
        """The kept name of each of ``principals`` that has one."""
        wanted = sorted(set(principals))
        if not wanted:
            return {}
        async with self._transaction() as conn:
            rows = await (await conn.execute("SELECT principal, name FROM people WHERE principal = ANY(%s)", (wanted,))).fetchall()
        return {str(p): str(n) for p, n in rows}

    async def newest_of_tenant(self, tenant: str, project_ids: Iterable[str], *, limit: int) -> list[str]:
        """Of ``project_ids``, those of ``tenant``, newest first, at most ``limit``: a bounded read of one indexed column
        (`projects_of_tenant`), no document and no count."""
        wanted = sorted(set(project_ids))
        if not wanted or limit <= 0:
            return []
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT project_id FROM projects WHERE tenant = %s AND project_id = ANY(%s) ORDER BY created_at DESC, project_id LIMIT %s",
                    (tenant, wanted, limit),
                )
            ).fetchall()
        return [str(r[0]) for r in rows]

    async def worked_replicas(self, project_id: str, subject: str) -> dict[str, set[str]]:
        """Each consensus group of the project ``subject`` holds, held or worked a replica of, with those replicas: what
        the one-replica-per-annotator rule reads (`fire_task`), in one statement for a whole page. The same facts the
        rule reads off the locked siblings: their holder, their holders and their submitter."""
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT replica_of, task_id FROM tasks WHERE project_id = %s AND replica_of IS NOT NULL "
                    "AND (assignee = %s OR submitted_by = %s OR holders @> jsonb_build_array(%s::text))",
                    (project_id, subject, subject, subject),
                )
            ).fetchall()
        groups: dict[str, set[str]] = {}
        for group, task_id in rows:
            groups.setdefault(str(group), set()).add(str(task_id))
        return groups

    async def publish_material(self, project_id: str) -> list[tuple[Task, Draft | None]]:
        """Every task of the project with its draft, in task-id order: what a publish builds its plan from."""
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT t.document, d.document, clock_timestamp() FROM tasks t LEFT JOIN drafts d USING (task_id) WHERE t.project_id = %s ORDER BY t.task_id",
                    (project_id,),
                )
            ).fetchall()
        material: list[tuple[Task, Draft | None]] = []
        for task_document, draft_document, now in rows:
            task = _task(task_document)
            lapse_expired_claim(task, now)
            material.append((task, None if draft_document is None else Draft.model_validate(draft_document)))
        return material

    # ---------------------------------------------------------------------------------------------
    # Task transitions
    # ---------------------------------------------------------------------------------------------

    async def _lock_task(self, conn: Connection, task_id: str, *, project_write: bool = False) -> tuple[Task, AnnotationProject, list[Task], datetime]:
        """Lock the task's project, then the task (with every replica of its consensus group, in task-id order), and
        answer the task, its project, its siblings and the database's clock, with a lapsed claim released.

        A task's project and group never change, so they are read before the locks that follow the one order.
        """
        found = await (await conn.execute("SELECT project_id, replica_of FROM tasks WHERE task_id = %s", (task_id,))).fetchone()
        if found is None:
            raise IllegalTransition("task", "absent", "any", message=f"task {task_id} does not exist")
        project_id, group = found
        project = await self._lock_project(conn, project_id, write=project_write)
        if group is None:
            rows = await (await conn.execute("SELECT document, clock_timestamp() FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,))).fetchall()
        else:
            rows = await (
                await conn.execute(
                    "SELECT document, clock_timestamp() FROM tasks WHERE project_id = %s AND replica_of = %s ORDER BY task_id FOR UPDATE",
                    (project_id, group),
                )
            ).fetchall()
        task: Task | None = None
        siblings: list[Task] = []
        now: datetime | None = None
        for document, clock in rows:
            row_task = _task(document)
            lapse_expired_claim(row_task, clock)
            now = clock
            if row_task.task_id == task_id:
                task = row_task
            else:
                siblings.append(row_task)
        if task is None or now is None:
            raise IllegalTransition("task", "absent", "any", message=f"task {task_id} does not exist")
        return task, project, siblings, now

    async def fire_task(
        self,
        task_id: str,
        *,
        event: str,
        actor: str,
        subject_can_manage: bool = False,
        lease_seconds: int | None = None,
        assignee: str | None = None,
        message: str = "",
        shape_ids: Iterable[str] = (),
    ) -> Task:
        """Apply one principal's edge of `TASK_EDGES` to the task, in one transaction.

        ``subject_can_manage`` is the checker's answer for ``actor`` on the task's project, stated by the layer that
        asked it (`release` is "lease holder or can_manage"). Everything else is read from the rows this transaction holds.
        """
        async with self._transaction() as conn:
            # `reopen` leaves `accepted`, which voids any canonical pick naming the task: a write to the project.
            task, project, siblings, now = await self._lock_task(conn, task_id, project_write=event == "reopen")

            target, permission = task_transition(task.state, event)
            if permission is None:
                raise IllegalTransition("task", task.state, f"{event} (caused by the system, never by a principal)")
            violation = identity_violation(
                event=event, subject=actor, assignee=task.assignee, submitted_by=task.submitted_by, subject_can_manage=subject_can_manage
            )
            if violation is not None:
                raise IllegalTransition("task", task.state, f"{event} ({violation})")
            _refuse_if_frozen(project, task, event)

            # Consensus v1: one replica per annotator per group, against the group's rows this transaction holds, so two
            # claims of two replicas by one person cannot both land. A sibling counts once its recipient ever held it,
            # not only while they hold it: a release does not unsee the work.
            recipient = (assignee or actor) if event == "assign" else actor
            if event in {"claim", "assign"} and task.replica_of:
                for sibling in siblings:
                    if recipient in (sibling.assignee, sibling.submitted_by, *sibling.holders):
                        who = "you hold, held or worked" if recipient == actor else "they hold, held or worked"
                        raise ConflictError(
                            f"one replica per annotator per group: {who} another replica of this item",
                            extensions={"principal": recipient, "replica": sibling.task_id, "group": task.replica_of},
                        )
            if event in {"claim", "assign"} and recipient not in task.holders:
                task.holders.append(recipient)

            if event == "submit":
                # Read from the TASK, captured at send from the project, never from the request: a caller-supplied
                # `review_required` would let an annotator self-accept. The draft is the submission, judged against
                # the ontology the task captured.
                target = submit_target(task.review_required)

            if event in SETTLES_PROPOSALS:
                # A proposal is a model's, and a submitted or accepted item is a person's: each is kept or removed first
                # (ADR 0019). Judged on the draft this transaction holds, at the submit and again at each acceptance,
                # since a reviewer may change the work in review.
                held = await _draft(conn, task_id)
                if unsettled := sum(1 for s in (held.shapes if held else []) if s.status == PROPOSED):
                    raise IllegalTransition("task", task.state, event, message=unsettled_proposals(unsettled, event))
                if event == "submit":
                    # The submission is the item's labels, the shapes a publish would write, judged against the
                    # ontology the task captured: a shape a person turned down neither satisfies a rule nor breaks one.
                    kept, joined = labels_of(held)
                    shapes = [ShapeLike.model_validate(s.model_dump()) for s in kept]
                    links = [LinkLike.model_validate(link.model_dump()) for link in joined]
                    if (contract := validate_against_ontology(task.ontology, shapes, links)) is not None:
                        raise IllegalTransition("task", task.state, f"submit ({contract})")

            task.transitions.append(Transition(at=now, by=actor, event=event, from_state=task.state, to_state=target))
            leaving_accepted = task.state is TaskState.ACCEPTED
            task.state = target
            if event == "claim":
                # A self-claim: the holder IS the actor. The duration falls back to the project's lease captured at send.
                task.assignee = actor
                task.lease_expires_at = now + _seconds(lease_seconds or task.lease_seconds)
            elif event == "assign":
                # A manager assigns to a NAMED person, and the assignment pins the task: no lease, because the assignee
                # did not choose when to start (§4.2).
                task.assignee = assignee or actor
                task.lease_expires_at = None
            elif event == "save_draft":
                task.lease_expires_at = now + _seconds(lease_seconds or task.lease_seconds)
            elif event == "submit":
                task.submitted_by, task.submitted_at = actor, now
                task.assignee, task.lease_expires_at = None, None
            elif event in {"release", "skip"}:
                task.assignee, task.lease_expires_at = None, None
            elif event in {"accept", "fix_and_accept", "request_changes"}:
                task.reviewed_by, task.reviewed_at = actor, now
                task.review_action = "accepted" if event == "accept" else event
                if event == "request_changes":
                    # The reviewer's reason travels with the task back to the annotator (§5.2).
                    task.review_notes.append(ReviewNote(by=actor, at=now, action="request_changes", message=message, shape_ids=[str(s) for s in shape_ids]))

            if leaving_accepted and any(pick.task_id == task_id for pick in project.adjudications.values()):
                # A pick is void once its target leaves `accepted`: a pick surviving reopen, resubmit and re-accept would
                # make canonical work the adjudicator never saw.
                project.adjudications = {group: pick for group, pick in project.adjudications.items() if pick.task_id != task_id}
                await self._write_project(conn, project)
            await _write_task(conn, task)
            return task

    async def change_draft(
        self,
        task_id: str,
        *,
        author: str,
        change: DraftChange,
        base_revision: int | None,
        require_base: bool = True,
        origin: DraftOrigin = "human",
        reviewing: bool = False,
        lease_seconds: int | None = None,
    ) -> Draft:
        """Apply ``change`` to the task's draft in one transaction, bumping its revision and recording it in the history.

        Writable only while the task takes edits (claimed, or in review for a reviewer the caller verified,
        ``reviewing``), only by who may edit it now (`machines.edit_violation`), and not once the project is frozen:
        each checked on the rows this transaction holds, the task's row locked. ``base_revision`` is the revision the
        caller built its change on (0 before the first save): another is a 409, per task, so two tabs cannot silently
        overwrite each other. ``change`` runs on the draft this transaction read and may refuse (it raises, and nothing
        is written). A save into a claim renews its lease.
        """

        async def changed(_conn: Connection, current: Draft | None, now: datetime) -> tuple[list[Shape], list[Link]]:
            return change(current, now)

        async with self._transaction() as conn:
            return await self._write_draft(
                conn,
                task_id,
                author=author,
                change=changed,
                base_revision=base_revision,
                require_base=require_base,
                origin=origin,
                reviewing=reviewing,
                lease_seconds=lease_seconds,
            )

    async def revert_save(self, task_id: str, *, author: str, revision: int, reviewing: bool = False) -> tuple[Draft, int]:
        """Undo the save that wrote ``revision`` of the task's draft, while the draft is still at that revision: the
        shapes it changed or removed come back whole (their provenance, status, scores, text, attributes and times) and
        its links as they were, from the undo its history kept (`ReverseChange`). Written as a save is, under the same
        rules and the task's row lock, as a new revision with its own undo, so undoing the revert redoes the save. A 409
        when the draft moved past ``revision`` or its history no longer keeps that save. Answers the draft written and how
        many shapes the undo put back or took away."""
        touched = 0

        async def reverted(conn: Connection, current: Draft | None, _now: datetime) -> tuple[list[Shape], list[Link]]:
            nonlocal touched
            row = await (await conn.execute("SELECT undo FROM draft_history WHERE task_id = %s AND revision = %s", (task_id, revision))).fetchone()
            if current is None or row is None:
                raise ConflictError(f"the history of this item's work no longer keeps the save that wrote v{revision}")
            undo = ReverseChange.model_validate(row[0])
            touched = len(undo.restore) + len(undo.remove)
            return undo.undo(current.shapes, current.links)

        async with self._transaction() as conn:
            draft = await self._write_draft(conn, task_id, author=author, change=reverted, base_revision=revision, reviewing=reviewing)
        return draft, touched

    async def _write_draft(
        self,
        conn: Connection,
        task_id: str,
        *,
        author: str,
        change: Callable[[Connection, Draft | None, datetime], Awaitable[tuple[list[Shape], list[Link]]]],
        base_revision: int | None,
        require_base: bool = True,
        origin: DraftOrigin = "human",
        reviewing: bool = False,
        lease_seconds: int | None = None,
    ) -> Draft:
        """`change_draft`'s write, inside the caller's transaction: the task's row locked, every rule of who may edit it
        now checked on it, the revision checked, ``change`` applied to the draft read, and the draft, its history and the
        claim's renewed lease written."""
        task, project, _siblings, now = await self._lock_task(conn, task_id)
        if task.state not in EDIT_PERMISSION or (task.state is TaskState.IN_REVIEW and not reviewing):
            raise IllegalTransition("draft", task.state.value, "save_draft")
        violation = edit_violation(state=task.state, subject=author, assignee=task.assignee, submitted_by=task.submitted_by)
        if violation is not None:
            raise IllegalTransition("draft", violation, "save_draft")
        _refuse_if_frozen(project, task, "save_draft")

        current = await _draft(conn, task_id)
        at = current.revision if current is not None else 0
        if base_revision is None:
            if require_base:
                raise ConflictError(
                    f"this save did not state the version it was built from; the work is at v{at}. Re-read it and send that version as base_version."
                )
        elif base_revision != at:
            raise ConflictError(f"annotations changed on the server (loaded v{base_revision}, now v{at})")
        shapes, links = await change(conn, current, now)
        if shapes == (current.shapes if current else []) and links == (current.links if current else []):
            # A change that changes nothing writes nothing: no revision, no history slot, no renewed lease.
            return current or Draft(task_id=task_id, project_id=task.project_id, author=author, revision=0)
        draft = Draft(
            task_id=task_id,
            project_id=task.project_id,
            author=author,
            shapes=shapes,
            links=links,
            revision=at + 1,
            updated_at=now,
            origin=origin,
        )
        await conn.execute(
            "INSERT INTO drafts (task_id, revision, document) VALUES (%s, %s, %s) "
            "ON CONFLICT (task_id) DO UPDATE SET revision = EXCLUDED.revision, document = EXCLUDED.document",
            (task_id, draft.revision, _stored_draft(draft)),
        )
        undo = storable_json("the save's history", ReverseChange.of(current, shapes, links).model_dump(mode="json"))
        await conn.execute(
            "INSERT INTO draft_history (task_id, revision, saved_at, author, shapes, bytes, undo) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (task_id, draft.revision, now, storable_text("the save's author", author), len(shapes), len(undo.encode()), undo),
        )
        await _trim_history(conn, task_id)
        if task.state is TaskState.CLAIMED:
            task.lease_expires_at = now + _seconds(lease_seconds or task.lease_seconds)
        await _write_task(conn, task)
        return draft

    async def draft_history(self, task_id: str, *, limit: int) -> list[DraftRevision]:
        """The task's kept saves, newest first, at most ``limit``."""
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT revision, saved_at, author, shapes FROM draft_history WHERE task_id = %s ORDER BY revision DESC LIMIT %s",
                    (task_id, limit),
                )
            ).fetchall()
        return [DraftRevision(revision=int(r[0]), saved_at=r[1], author=str(r[2]), shapes=int(r[3])) for r in rows]

    async def draft_at(self, task_id: str, revision: int) -> Draft | None:
        """The task's draft as ``revision`` left it, rebuilt from the draft by undoing every later save, or `None` when the
        history does not keep it (never saved, or past the bound)."""
        async with self._transaction() as conn:
            current = await _draft(conn, task_id)
            rows = await (
                await conn.execute(
                    "SELECT revision, saved_at, author, undo FROM draft_history WHERE task_id = %s AND revision >= %s ORDER BY revision DESC",
                    (task_id, revision),
                )
            ).fetchall()
        if current is None or not rows or int(rows[-1][0]) != revision:
            return None
        shapes, links = current.shapes, current.links
        for row in rows[:-1]:
            shapes, links = ReverseChange.model_validate(row[3]).undo(shapes, links)
        _rev, saved_at, author, _undo = rows[-1]
        return Draft(task_id=task_id, project_id=current.project_id, author=str(author), shapes=shapes, links=links, revision=revision, updated_at=saved_at)


async def _update_unstarted(conn: Connection, project_id: str, fields: dict[str, Any]) -> None:
    """Write ``fields`` into every task of the project nobody has started: one still unassigned, never held (a release
    leaves its holder in `holders`) and with no draft. A task someone claimed, drew on or submitted keeps what it captured
    at send, so a setting changed mid-flight never re-judges work already done. Called under the project's write lock,
    which every task transition and every send waits on, so no task starts between the read and the write."""
    await conn.execute(
        "UPDATE tasks t SET document = t.document || %s "
        "WHERE t.project_id = %s AND t.state = 'unassigned' "
        "AND jsonb_array_length(coalesce(t.document -> 'holders', '[]'::jsonb)) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.task_id = t.task_id)",
        (_stored("the project's settings", fields), project_id),
    )


def _captured(project: AnnotationProject, tasks: list[Task]) -> list[Task]:
    """The tasks as the project held now makes them: each stamped with its review rule, claim lease and ontology, its
    predictions judged against that ontology, and on a consensus project a single item made into its replicas."""
    out: list[Task] = []
    for index, task in enumerate(tasks):
        violation = membership_violation(project.ontology, [ShapeLike.model_validate(s.model_dump()) for s in task.prediction])
        if violation is not None:
            raise ConflictError(f"item {index + 1} of {len(tasks)}: {violation}")
        captured = task.model_copy(
            update={"review_required": project.review_required, "lease_seconds": project.lease_seconds, "ontology": project.ontology.model_copy(deep=True)}
        )
        if project.consensus_n == 1 or task.replica_of is not None:
            out.append(captured)
            continue
        out.extend(
            captured.model_copy(update={"task_id": f"{task.task_id}-r{k}", "replica_of": task.task_id}, deep=True) for k in range(1, project.consensus_n + 1)
        )
    return out


async def _trim_history(conn: Connection, task_id: str) -> None:
    """Keep the task's newest saves: at most `MAX_DRAFT_HISTORY`, and as many of them as `MAX_HISTORY_BYTES` holds."""
    rows = await (await conn.execute("SELECT revision, bytes FROM draft_history WHERE task_id = %s ORDER BY revision DESC", (task_id,))).fetchall()
    total, oldest = 0, None
    for count, (rev, size) in enumerate(rows, start=1):
        total += int(size)
        if count > MAX_DRAFT_HISTORY or (total > MAX_HISTORY_BYTES and count > 1):
            break
        oldest = int(rev)
    if oldest is not None:
        await conn.execute("DELETE FROM draft_history WHERE task_id = %s AND revision < %s", (task_id, oldest))


def _is_replica_of(task_id: str, group: str) -> bool:
    prefix = f"{group}-r"
    return task_id.startswith(prefix) and task_id[len(prefix) :].isdigit()


def _seconds(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)


async def _clock(conn: Connection) -> datetime:
    """The database's clock: the one every replica's leases and stamps are read against."""
    row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    if row is None:
        raise RuntimeError("the database answered no clock")
    return cast("datetime", row[0])


async def _draft(conn: Connection, task_id: str) -> Draft | None:
    row = await (await conn.execute("SELECT document FROM drafts WHERE task_id = %s", (task_id,))).fetchone()
    return None if row is None else Draft.model_validate(row[0])


def _task_row(task: Task) -> tuple[Any, ...]:
    # The document is checked first: every text column below is a copy of one of its values.
    document = _stored("the task", task.model_dump(mode="json"))
    return (task.task_id, task.project_id, str(task.state), task.assignee, task.lease_expires_at, task.replica_of, document)


async def _write_task(conn: Connection, task: Task) -> None:
    # The history is bounded here, at the one seam every task write goes through: the document is rewritten whole, so an
    # unbounded trail would make each event cost more than the last.
    task.trim_history()
    document = _stored("the task", task.model_dump(mode="json"))
    await conn.execute(
        "UPDATE tasks SET state = %s, assignee = %s, lease_expires_at = %s, document = %s WHERE task_id = %s",
        (str(task.state), task.assignee, task.lease_expires_at, document, task.task_id),
    )


async def _task_index(conn: Connection, project_id: str) -> TaskIndex:
    """How many of the project's tasks are in each state, as a reader sees them, and the publish precondition: one
    aggregate over the project's rows, whatever their number, never a list of them."""
    rows = await (await conn.execute("SELECT state, count(*) FROM task_states WHERE project_id = %s GROUP BY state", (project_id,))).fetchall()
    counts = dict.fromkeys(TaskState, 0)
    for state, count in rows:
        counts[TaskState(state)] = int(count)
    total = sum(counts.values())
    terminal = sum(counts[s] for s in TERMINAL_TASK_STATES)
    return TaskIndex(counts=counts, total=total, terminal=terminal, may_publish=terminal == total)
