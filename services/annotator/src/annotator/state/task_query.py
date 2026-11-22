"""The data manager's read of a project's items: filtered, counted, sorted and paged in Postgres.

A project accumulates an item per sent row per replica for its whole life, so a page of it is read the way a table is,
never by reading every item into the browser: the filters below are SQL over the narrow columns `schema.sql` generates
from each task's document, the order is stable (the chosen key, then the task id), and a page is a bounded offset into
it. Every number one answer carries (the page, how many match, the count per state, where one item sits) is read in
ONE repeatable-read snapshot, so a page and its counts never disagree with one another.

A claim past its expiry is unassigned here as everywhere (`task_states`): the state and assignee filtered and sorted on
are the ones a reader sees.

Values are bound as parameters; a column or a direction is chosen from the closed enums below, never from the request.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Final

from psycopg import AsyncConnection, sql
from pydantic import AwareDatetime, BaseModel, Field

from annotator.judgement import Judgement
from annotator.projects.models import SampleReason, SamplingStrategy, TaskState
from annotator.sampling import ItemSampling, Novelty, Sampling


type Connection = AsyncConnection[tuple[Any, ...]]

#: The project's datasets, read as a loose index scan of `tasks_by_dataset`: one probe per distinct dataset, however many
#: items each holds (a plain DISTINCT reads an entry per item).
_DATASETS: Final = (
    "WITH RECURSIVE found AS ("
    " (SELECT dataset FROM tasks WHERE project_id = %(project)s AND dataset IS NOT NULL ORDER BY dataset LIMIT 1)"
    " UNION ALL"
    " SELECT (SELECT t.dataset FROM tasks t WHERE t.project_id = %(project)s AND t.dataset > found.dataset ORDER BY t.dataset LIMIT 1)"
    " FROM found WHERE found.dataset IS NOT NULL"
    ") SELECT dataset FROM found WHERE dataset IS NOT NULL LIMIT %(limit)s"
)

#: The deepest offset a listing reads at: past it a person narrows the filter rather than pages. A project's items are
#: an index scan on (project, task id) away, so the bound is on what an answer may cost, not on what a project may hold.
MAX_OFFSET: Final = 100_000
#: The most datasets one answer names for the filter by dataset.
MAX_DATASETS: Final = 200
#: The longest value a text filter takes (a principal, a class name, a dataset id, a group id).
MAX_FILTER: Final = 512


class TaskSort(StrEnum):
    """What a listing is ordered by. `item` is the task id, the order items were minted in within one send."""

    ITEM = "item"
    STATE = "state"
    ASSIGNEE = "assignee"
    SENT = "sent"
    UPDATED = "updated"
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"
    #: What a judge said of each item's submission (ADR 0026): the flagged first, then those no judge has read, then
    #: those it found nothing in; within each, the longest waiting first.
    JUDGE = "judge"
    #: The project's sampling strategy (ADR 0034): under `least_sure` the items its sampling model is least sure about
    #: first, then those it proposed nothing on, then those it has not scored; under `least_alike` the items least like
    #: the labelled ones first, then those not measured; under `mix` the two orders interleaved, doubt's first. Items
    #: with nothing to order by, and every item while sampling is off, are in item order.
    SAMPLING = "sampling"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class DateField(StrEnum):
    """Which of an item's instants a date range filters: when it was sent, last moved, submitted or reviewed."""

    SENT = "sent"
    UPDATED = "updated"
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"


#: The work order a person reads states in: what waits for someone first, what is finished last.
STATE_ORDER: Final = (
    TaskState.UNASSIGNED,
    TaskState.CLAIMED,
    TaskState.CHANGES_REQUESTED,
    TaskState.IN_REVIEW,
    TaskState.ACCEPTED,
    TaskState.SKIPPED,
)

_SORT_KEY: Final = {
    TaskSort.ITEM: sql.SQL("task_id"),
    TaskSort.STATE: sql.SQL("array_position({}::text[], state)").format(sql.Literal([str(s) for s in STATE_ORDER])),
    TaskSort.ASSIGNEE: sql.SQL("assignee"),
    TaskSort.SENT: sql.SQL("sent_at"),
    TaskSort.UPDATED: sql.SQL("updated_at"),
    TaskSort.SUBMITTED: sql.SQL("submitted_at"),
    TaskSort.REVIEWED: sql.SQL("reviewed_at"),
}
_DATE_COLUMN: Final = {
    DateField.SENT: sql.SQL("sent_at"),
    DateField.UPDATED: sql.SQL("updated_at"),
    DateField.SUBMITTED: sql.SQL("submitted_at"),
    DateField.REVIEWED: sql.SQL("reviewed_at"),
}

#: The model the project samples by now: a score another model gave reads as none.
_SAMPLED_BY: Final = "(SELECT p.document ->> 'sampling_model' FROM projects p WHERE p.project_id = {project})"

#: A project's items as a reader sees them, one row each: a lapsed claim is unassigned and held by nobody. The clock is
#: the transaction's (`now()`, the start of the snapshot every number of one answer is read in), which is STABLE: a
#: volatile clock would make Postgres materialise this whole subquery for each statement rather than fold the filter,
#: the order and the limit into it, and so read the project's every row for a page of 25.
_ITEMS: Final = sql.SQL(
    "SELECT t.task_id, t.replica_of, "
    "CASE WHEN t.state = 'claimed' AND t.lease_expires_at <= now() THEN 'unassigned' ELSE t.state END AS state, "
    "CASE WHEN t.state = 'claimed' AND t.lease_expires_at <= now() THEN NULL ELSE t.assignee END AS assignee, "
    "t.dataset, t.submitted_by, t.reviewed_by, t.review_action, t.holders, t.predicted, "
    "t.sent_at, t.submitted_at, t.reviewed_at, t.updated_at, "
    # Whether a judge flagged the item's submission of now; NULL where none has read it. A statement that does not name
    # the column never runs the subquery.
    "(SELECT j.flagged FROM judgements j WHERE j.task_id = t.task_id AND j.submitted_at = t.submitted_at) AS flagged, "
    # The doubt of the project's sampling model of now on the item (NULL: it proposed nothing the item takes, or has not
    # scored it), and whether it scored it; read only by a statement that names them, as `flagged` is.
    f"(SELECT s.doubt FROM samplings s WHERE s.task_id = t.task_id AND s.model = {_SAMPLED_BY}) AS doubt, "
    f"EXISTS (SELECT 1 FROM samplings s WHERE s.task_id = t.task_id AND s.model = {_SAMPLED_BY}) AS scored, "
    # How unlike the labelled items the item was when it was last measured; NULL until it is.
    "(SELECT n.novelty FROM novelties n WHERE n.task_id = t.task_id) AS novelty "
    "FROM tasks t WHERE t.project_id = {project}"
)

#: Where an item sits in the judge's order: flagged, unread, clear.
_JUDGE_RANK: Final = sql.SQL("CASE WHEN flagged THEN 0 WHEN flagged IS NULL THEN 1 ELSE 2 END")
#: Where an item sits in the least-sure order: scored with a doubt, scored with none (the model proposed nothing), unscored.
_DOUBT_RANK: Final = sql.SQL("CASE WHEN doubt IS NOT NULL THEN 0 WHEN scored THEN 1 ELSE 2 END")
#: Where an item sits in the least-alike order: measured, not measured.
_NOVELTY_RANK: Final = sql.SQL("CASE WHEN novelty IS NOT NULL THEN 0 ELSE 1 END")
#: An item's place in the mixed order among the items a filter matches: the doubt order takes the odd places and the
#: novelty order the even ones, and an item in both stands at the earlier. NULL for an item with neither number.
_MIX_PLACE: Final = sql.SQL(
    "LEAST("
    "CASE WHEN doubt IS NOT NULL THEN 2 * row_number() OVER (PARTITION BY doubt IS NOT NULL ORDER BY doubt DESC, task_id) - 1 END, "
    "CASE WHEN novelty IS NOT NULL THEN 2 * row_number() OVER (PARTITION BY novelty IS NOT NULL ORDER BY novelty DESC, task_id) END)"
)


class TaskFilter(BaseModel):
    """Which of a project's items a listing answers. Every field narrows; none set is the whole project."""

    states: list[TaskState] = Field(default_factory=list, max_length=len(TaskState))
    assignee: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    reviewer: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    submitter: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: Items someone other than this person submitted: a reviewer's pass leaves out their own work, which they may not
    #: review (`machines.identity_violation`).
    not_submitter: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: Items this person ever held (claimed or was assigned) or submitted: their own work.
    holder: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: Items open to this person's labeling now: their live claims, and the items nobody holds (unassigned, or sent back
    #: and so claimable again). It narrows the listing only: what they may fire on each item is its `legal_events`.
    open_to: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: A class on the item: on a shape of its work, or proposed for it at send.
    label: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    dataset: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: One consensus group's replicas.
    group: str | None = Field(default=None, min_length=1, max_length=MAX_FILTER)
    #: Only the items that are replicas of a consensus group.
    replicas: bool = False
    #: With true, only the items whose work holds something: at least one shape or link in their draft (a browse looks
    #: through labeled work, and an item claimed and never drawn on has none); with false, only the items whose work holds
    #: nothing (no draft, or one emptied again); unset, both.
    has_work: bool | None = None
    #: With false, only the items the project's sampling model of now has not scored (what a visit reads a score
    #: column for and asks the model about); with true, only those it has; unset, both.
    scored: bool | None = None
    #: With false, only the items not measured against the labelled ones (what a visit measures); with true, only those
    #: that are; unset, both.
    measured: bool | None = None
    date_field: DateField = DateField.UPDATED
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None

    def clauses(self, *, with_states: bool = True) -> sql.Composed:
        """The filter as SQL over `_ITEMS`' columns, its values bound; ``with_states=False`` leaves the state out (the
        per-state counts are read under every other filter)."""
        parts: list[sql.Composable] = [sql.SQL("TRUE")]
        if with_states and self.states:
            parts.append(sql.SQL("state = ANY({})").format(sql.Literal([str(s) for s in self.states])))
        for column, value in (("assignee", self.assignee), ("reviewed_by", self.reviewer), ("submitted_by", self.submitter), ("dataset", self.dataset)):
            if value is not None:
                parts.append(sql.SQL("{} = {}").format(sql.Identifier(column), sql.Literal(value)))
        if self.group is not None:
            parts.append(sql.SQL("replica_of = {}").format(sql.Literal(self.group)))
        if self.replicas:
            parts.append(sql.SQL("replica_of IS NOT NULL"))
        if self.not_submitter is not None:
            parts.append(sql.SQL("submitted_by IS DISTINCT FROM {}").format(sql.Literal(self.not_submitter)))
        if self.holder is not None:
            parts.append(sql.SQL("(holders @> jsonb_build_array({0}::text) OR submitted_by = {0})").format(sql.Literal(self.holder)))
        if self.open_to is not None:
            parts.append(sql.SQL("(state IN ('unassigned', 'changes_requested') OR (state = 'claimed' AND assignee = {}))").format(sql.Literal(self.open_to)))
        if self.label is not None:
            parts.append(
                sql.SQL(
                    "(predicted @> jsonb_build_array({0}::text) "
                    "OR EXISTS (SELECT 1 FROM drafts d WHERE d.task_id = items.task_id AND d.labels @> jsonb_build_array({0}::text)))"
                ).format(sql.Literal(self.label))
            )
        if self.has_work is not None:
            worked = sql.SQL("EXISTS (SELECT 1 FROM drafts d WHERE d.task_id = items.task_id AND d.has_work)")
            parts.append(worked if self.has_work else sql.SQL("NOT {}").format(worked))
        if self.scored is not None:
            parts.append(sql.SQL("scored" if self.scored else "NOT scored"))
        if self.measured is not None:
            parts.append(sql.SQL("novelty IS NOT NULL" if self.measured else "novelty IS NULL"))
        column = _DATE_COLUMN[self.date_field]
        if self.since is not None:
            parts.append(sql.SQL("{} >= {}").format(column, sql.Literal(self.since)))
        if self.until is not None:
            parts.append(sql.SQL("{} < {}").format(column, sql.Literal(self.until)))
        return sql.SQL(" AND ").join(parts)


class TaskPage(BaseModel):
    """One page of a filtered project: its task ids in order, and the numbers of the same snapshot."""

    task_ids: list[str]
    #: How many items the whole filter matches.
    matched: int
    #: How many items each state holds under every filter but the state one: what choosing a state would leave.
    state_counts: dict[TaskState, int]
    #: Where this page starts in the filtered order.
    offset: int
    #: Where ``around``'s item sits in the filtered order, or None when the filter leaves it out (or none was named).
    position: int | None = None
    #: The datasets the project's items come from, what the filter by dataset offers (at most `MAX_DATASETS`).
    datasets: list[str] = Field(default_factory=list)
    #: Each listed item's work, by task id, when the read asked for it (`AnnotatorStore.task_page`'s ``work``); an item
    #: with no draft yet is absent.
    work: dict[str, ItemWork] = Field(default_factory=dict)
    #: Each listed item's verdict on its submission of now, by task id, when the read asked for it; an item no judge has
    #: read is absent. And how many of the items the filter matches are flagged, clear and unread.
    judgements: dict[str, Judgement] = Field(default_factory=dict)
    judged: JudgeCounts | None = None
    #: What sampling knows of each listed item, by task id, when the read asked for it; an item with neither a score by
    #: the project's sampling model of now nor a novelty is absent. And how many of the items the filter matches have each.
    samplings: dict[str, ItemSampling] = Field(default_factory=dict)
    sampled: SamplingCounts | None = None
    #: Which number placed each listed item in a listing ordered by the strategy, by task id.
    placed: dict[str, SampleReason] = Field(default_factory=dict)


class PageExtras(BaseModel):
    """What a page carries beside its items: each listed item's work, each one's verdict and each one's sampling score
    with the filter's counts of them, and each person's part in the project."""

    work: bool = False
    judge: bool = False
    sampling: bool = False
    contributors: bool = False


class JudgeCounts(BaseModel):
    """How a judge has read the items a filter matches: how many it flagged, how many it found nothing in, and how many
    no judge has read."""

    flagged: int = 0
    clear: int = 0
    unread: int = 0


async def judgements_of(conn: Connection, *, project_id: str, filters: TaskFilter, task_ids: list[str]) -> tuple[dict[str, Judgement], JudgeCounts]:
    """The verdict on the submission of now of each of ``task_ids`` that has one, and the counts over everything
    ``filters`` matches."""
    rows = await (
        await conn.execute(
            "SELECT j.document FROM judgements j JOIN tasks t ON t.task_id = j.task_id AND t.submitted_at = j.submitted_at WHERE j.task_id = ANY(%s)",
            (task_ids,),
        )
    ).fetchall()
    verdicts = [Judgement.model_validate(row[0]) for row in rows]
    items = _ITEMS.format(project=sql.Literal(project_id))
    counted = await (
        await conn.execute(
            sql.SQL("WITH items AS ({}) SELECT {} AS rank, count(*) FROM items WHERE {} GROUP BY 1").format(items, _JUDGE_RANK, filters.clauses())
        )
    ).fetchall()
    by_rank = {int(rank): int(count) for rank, count in counted}
    return {v.task_id: v for v in verdicts}, JudgeCounts(flagged=by_rank.get(0, 0), unread=by_rank.get(1, 0), clear=by_rank.get(2, 0))


class SamplingCounts(BaseModel):
    """Of the items a filter matches, how many the project's sampling model of now has scored and not, and how many are
    measured against the labelled items and not."""

    scored: int = 0
    unscored: int = 0
    measured: int = 0
    unmeasured: int = 0


async def samplings_of(
    conn: Connection, *, project_id: str, filters: TaskFilter, task_ids: list[str], placed: dict[str, SampleReason]
) -> tuple[dict[str, ItemSampling], SamplingCounts]:
    """What sampling knows of each of ``task_ids`` (its score by the project's sampling model of now, its novelty, and
    which of them ``placed`` it), and the counts over everything ``filters`` matches."""
    by = sql.SQL(_SAMPLED_BY).format(project=sql.Literal(project_id))
    scored = await (
        await conn.execute(sql.SQL("SELECT s.document FROM samplings s WHERE s.task_id = ANY({}) AND s.model = {}").format(sql.Literal(task_ids), by))
    ).fetchall()
    measured = await (await conn.execute("SELECT document FROM novelties WHERE task_id = ANY(%s)", (task_ids,))).fetchall()
    known = {task_id: ItemSampling(placed_by=reason) for task_id, reason in placed.items()}
    for (document,) in scored:
        score = Sampling.model_validate(document)
        known.setdefault(score.task_id, ItemSampling()).score = score
    for (document,) in measured:
        novelty = Novelty.model_validate(document)
        known.setdefault(novelty.task_id, ItemSampling()).novelty = novelty
    items = _ITEMS.format(project=sql.Literal(project_id))
    counted = await (
        await conn.execute(
            sql.SQL(
                "WITH items AS ({}) SELECT count(*) FILTER (WHERE scored), count(*) FILTER (WHERE novelty IS NOT NULL), count(*) FROM items WHERE {}"
            ).format(items, filters.clauses())
        )
    ).fetchone()
    with_score, with_novelty, total = (int(n) for n in counted) if counted else (0, 0, 0)
    return known, SamplingCounts(scored=with_score, unscored=total - with_score, measured=with_novelty, unmeasured=total - with_novelty)


#: The most whole-item tags one item's work answers in a listing (`ItemWork.tags`): a bulk label reads an item's tags to
#: replace them, and an item past this many is one to open on the canvas.
MAX_WORK_TAGS: Final = 64


class WorkTag(BaseModel):
    """One whole-item tag of an item's work: its shape id, its class, whether it carries more than its class (text,
    attributes, or a link to or from it: what a new label would discard), and whether it is a proposal no person has kept
    (`prediction`)."""

    id: str
    label: str | None
    detailed: bool
    prediction: bool


class ItemWork(BaseModel):
    """What a bulk label reads of one item's work: the draft's revision (0 before its first save), which a change states as
    the revision it was built on, its whole-item tags, at most `MAX_WORK_TAGS` (`tags_cut` when it holds more), and how
    many proposals of any shape it holds that no person kept or removed (an item holding one cannot be submitted)."""

    revision: int
    tags: list[WorkTag]
    tags_cut: bool
    proposals: int


#: Each listed item's draft revision, its first tags and its count of proposals, read from the drafts' generated
#: `work_tags` and `proposals` columns (`schema.sql`, `annotator_work_tags`) so a listing never reads a draft's whole document; an item with no draft has no row here.
_WORK: Final = (
    "SELECT d.task_id, d.revision, jsonb_path_query_array(d.work_tags, '$[0 to $last]', jsonb_build_object('last', %(cap)s - 1)), "
    "jsonb_array_length(d.work_tags), d.proposals "
    "FROM drafts d WHERE d.task_id = ANY(%(ids)s)"
)


async def work_of(conn: Connection, task_ids: list[str]) -> dict[str, ItemWork]:
    """The work of each of ``task_ids`` that has a draft, keyed by task id."""
    rows = await (await conn.execute(_WORK, {"ids": task_ids, "cap": MAX_WORK_TAGS})).fetchall()
    return {
        str(task_id): ItemWork(
            revision=int(revision), tags=[WorkTag.model_validate(t) for t in tags], tags_cut=int(count) > MAX_WORK_TAGS, proposals=int(proposals)
        )
        for task_id, revision, tags, count, proposals in rows
    }


class Contributor(BaseModel):
    """One person's part in a project, over all its items (the manager's per-person summary)."""

    principal: str
    #: Items they submitted (to review, or straight to accepted where the project takes no review).
    submitted: int = 0
    #: Of those, how many are accepted.
    accepted: int = 0
    #: Of those, how many a review sent back.
    changes_requested: int = 0
    #: Items they hold now and have not submitted.
    open: int = 0
    #: Items they reviewed.
    reviewed: int = 0


#: States an item still sits with its holder in.
_OPEN_STATES: Final = frozenset({TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.CHANGES_REQUESTED})


#: Each sampling strategy's order over `listed` (`page_of`): the rank that groups the items, and their order within a
#: group. A strategy absent here lists items in item order.
_SAMPLING_ORDER: Final = {
    SamplingStrategy.LEAST_SURE: (_DOUBT_RANK, sql.SQL("doubt DESC NULLS LAST")),
    SamplingStrategy.LEAST_ALIKE: (_NOVELTY_RANK, sql.SQL("novelty DESC NULLS LAST")),
    SamplingStrategy.MIX: (sql.SQL("CASE WHEN mix_place IS NOT NULL THEN 0 ELSE 1 END"), sql.SQL("mix_place ASC NULLS LAST")),
}
#: Which number placed an item, under each strategy.
_PLACED_BY: Final = {
    SamplingStrategy.LEAST_SURE: sql.SQL("CASE WHEN doubt IS NOT NULL THEN 'least_sure' END"),
    SamplingStrategy.LEAST_ALIKE: sql.SQL("CASE WHEN novelty IS NOT NULL THEN 'least_alike' END"),
    SamplingStrategy.MIX: sql.SQL("CASE WHEN mix_place % 2 = 1 THEN 'least_sure' WHEN mix_place % 2 = 0 THEN 'least_alike' END"),
}


async def _strategy_of(conn: Connection, project_id: str) -> SamplingStrategy:
    """The project's sampling strategy, read in the listing's own snapshot."""
    row = await (await conn.execute("SELECT document ->> 'sampling' FROM projects WHERE project_id = %s", (project_id,))).fetchone()
    return SamplingStrategy(row[0]) if row is not None and row[0] is not None else SamplingStrategy.OFF


def _order(sort: TaskSort, order: SortOrder, strategy: SamplingStrategy) -> sql.Composed:
    direction = sql.SQL("DESC") if order is SortOrder.DESC else sql.SQL("ASC")
    if sort is TaskSort.SAMPLING and strategy in _SAMPLING_ORDER:
        rank, within = _SAMPLING_ORDER[strategy]
        return sql.SQL("{} {}, {}, task_id ASC").format(rank, direction, within)
    if sort in (TaskSort.ITEM, TaskSort.SAMPLING):
        return sql.SQL("task_id {}").format(direction)
    if sort is TaskSort.JUDGE:
        return sql.SQL("{} {}, submitted_at ASC NULLS LAST, task_id ASC").format(_JUDGE_RANK, direction)
    # NULLS LAST either way: an item with no such instant (or no assignee) is never what a person sorted to find.
    return sql.SQL("{} {} NULLS LAST, task_id ASC").format(_SORT_KEY[sort], direction)


async def page_of(
    conn: Connection,
    project_id: str,
    filters: TaskFilter,
    *,
    sort: TaskSort,
    order: SortOrder,
    limit: int,
    offset: int,
    around: str | None = None,
) -> TaskPage:
    """One page of the project's items under ``filters``, in ``sort`` order, and the counts of the same snapshot.

    With ``around``, the page is the one that holds that item near its middle (the item's neighbours, for walking
    a filtered set one item at a time), and ``offset`` is ignored; an item the filter leaves out pages from ``offset``.
    The caller runs this inside one transaction (`AnnotatorStore.task_page` makes it a repeatable-read one).
    """
    items = _ITEMS.format(project=sql.Literal(project_id))
    where = filters.clauses()
    strategy = await _strategy_of(conn, project_id) if sort is TaskSort.SAMPLING else SamplingStrategy.OFF
    ordering = _order(sort, order, strategy)
    # The items the filter matches, each with its place in the mixed order where the listing is ordered by it: a place
    # counts over the matched items, so it is worked out after the filter and before the page is cut.
    listed = sql.SQL("SELECT items.*, {} AS mix_place FROM items WHERE {}").format(
        _MIX_PLACE if strategy is SamplingStrategy.MIX else sql.SQL("NULL::bigint"), where
    )
    placed_by = _PLACED_BY.get(strategy, sql.SQL("NULL"))
    matched_row = await (await conn.execute(sql.SQL("WITH items AS ({}) SELECT count(*) FROM items WHERE {}").format(items, where))).fetchone()
    matched = int(matched_row[0]) if matched_row else 0
    counts_rows = await (
        await conn.execute(
            sql.SQL("WITH items AS ({}) SELECT state, count(*) FROM items WHERE {} GROUP BY state").format(items, filters.clauses(with_states=False))
        )
    ).fetchall()
    state_counts = dict.fromkeys(TaskState, 0)
    for state, count in counts_rows:
        state_counts[TaskState(state)] = int(count)
    position: int | None = None
    if around is not None:
        found = await (
            await conn.execute(
                sql.SQL(
                    "WITH items AS ({}), listed AS ({}) "
                    "SELECT pos FROM (SELECT task_id, row_number() OVER (ORDER BY {}) - 1 AS pos FROM listed) x WHERE task_id = {}"
                ).format(items, listed, ordering, sql.Literal(around))
            )
        ).fetchone()
        if found is not None:
            position = int(found[0])
            offset = max(0, position - limit // 2)
    rows = await (
        await conn.execute(
            sql.SQL("WITH items AS ({}), listed AS ({}) SELECT task_id, {} FROM listed ORDER BY {} LIMIT {} OFFSET {}").format(
                items, listed, placed_by, ordering, sql.Literal(limit), sql.Literal(offset)
            )
        )
    ).fetchall()
    datasets = await (await conn.execute(_DATASETS, {"project": project_id, "limit": MAX_DATASETS})).fetchall()
    return TaskPage(
        task_ids=[str(r[0]) for r in rows],
        matched=matched,
        state_counts=state_counts,
        offset=offset,
        position=position,
        datasets=[str(r[0]) for r in datasets],
        placed={str(r[0]): SampleReason(r[1]) for r in rows if r[1] is not None},
    )


async def contributors_of(conn: Connection, project_id: str) -> list[Contributor]:
    """Each person's part in the project over all its items, busiest first, ties by principal."""
    items = _ITEMS.format(project=sql.Literal(project_id))
    rows = await (
        await conn.execute(sql.SQL("WITH items AS ({}) SELECT state, assignee, submitted_by, reviewed_by, review_action FROM items").format(items))
    ).fetchall()
    people: dict[str, Contributor] = {}

    def of(principal: str) -> Contributor:
        return people.setdefault(principal, Contributor(principal=principal))

    for state_text, assignee, submitted_by, reviewed_by, review_action in rows:
        state = TaskState(state_text)
        # Whoever submitted an item owns the submission, not whoever holds it now.
        if submitted_by:
            row = of(submitted_by)
            row.submitted += 1
            if state is TaskState.ACCEPTED:
                row.accepted += 1
            elif review_action == "request_changes" or state is TaskState.CHANGES_REQUESTED:
                row.changes_requested += 1
        if assignee and not submitted_by and state in _OPEN_STATES:
            of(assignee).open += 1
        if reviewed_by:
            of(reviewed_by).reviewed += 1
    return sorted(people.values(), key=lambda c: (-c.submitted, -c.open, -c.reviewed, c.principal))


def principals_in(documents: Iterable[dict[str, Any]]) -> set[str]:
    """Every principal a task document names: who holds it, held it, submitted, reviewed, noted or moved it."""
    found: set[str] = set()
    for doc in documents:
        found.update(p for p in (doc.get("assignee"), doc.get("submitted_by"), doc.get("reviewed_by")) if isinstance(p, str))
        found.update(p for p in doc.get("holders") or [] if isinstance(p, str))
        for entry in [*(doc.get("review_notes") or []), *(doc.get("transitions") or [])]:
            if isinstance(entry, dict) and isinstance(entry.get("by"), str):
                found.add(entry["by"])
    return found


__all__ = [
    "MAX_FILTER",
    "MAX_OFFSET",
    "MAX_WORK_TAGS",
    "Contributor",
    "DateField",
    "ItemWork",
    "JudgeCounts",
    "PageExtras",
    "SamplingCounts",
    "SortOrder",
    "TaskFilter",
    "TaskPage",
    "TaskSort",
    "WorkTag",
    "contributors_of",
    "judgements_of",
    "page_of",
    "principals_in",
    "samplings_of",
    "work_of",
]
