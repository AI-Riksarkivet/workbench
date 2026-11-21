"""The two state machines of an annotation project (the project's and each task's), as data.

The transition tables ARE the spec — `fire()` consults them rather than reimplementing them in
branches, so "everything not in the table is illegal" is structural. Each edge also carries the
`can_*` permission that gates it, which keeps the op→privilege mapping in one place and lets the API
layer ask the model rather than hardcode a ladder.

Authorization is NOT performed here: this module says *which* permission an event requires, and the
caller checks it (`annotator.api.security`). That split is what lets the domain core be tested without a
store or a catalog.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal, NoReturn

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from annotator.projects.models import ProjectState, Task, TaskState, Transition
from workbench.exceptions import ConflictError


#: The events an item holding a proposal nobody kept or removed is refused (ADR 0019): the one that hands it to review,
#: and each that accepts it.
SETTLES_PROPOSALS: Final = frozenset({"submit", "accept", "fix_and_accept"})


def unsettled_proposals(count: int, event: str) -> str:
    """Why an item holding ``count`` proposals nobody kept or removed is not submitted or accepted, as a person reads it."""
    doing = "submitting" if event == "submit" else "accepting"
    if count == 1:
        return f"1 proposal on this item is neither kept nor removed: keep or remove it before {doing}"
    return f"{count} proposals on this item are neither kept nor removed: keep or remove each before {doing}"


class IllegalTransition(ConflictError):
    """Raised for an edge absent from the table — the closed-world guarantee of §5 — and for a transition whose
    preconditions the task's or project's own rows refuse. A 409 wherever it is raised, with no route translating it;
    ``str()`` is the sentence it carries, and its problem type is a conflict's."""

    problem_type = "about:blank#conflicterror"

    def __init__(self, kind: str, state: str, event: str, *, message: str | None = None) -> None:
        # The state's value, not an enum member's repr: the sentence reaches a person.
        self.kind = str(kind)
        self.state = str(state)
        self.event = str(event)
        super().__init__(message if message is not None else f"illegal {self.kind} transition: {self.event!r} is not permitted from {self.state!r}")


class UnreadableOntology(ConflictError):
    """A PERSISTED annotation document whose ontology the running models cannot parse.

    **Why this is a refusal and not a shrug.** The ontology is a CLOSED-SET contract: `send`,
    `import`, `assist` and `submit` judge every shape against it and the publish stamps its class
    list into the run facet. Reading an unparseable ontology as `LabelOntology()` — "constrains
    nothing" — does not degrade the contract, it DELETES it, silently, on exactly the paths where
    one click is hundreds of rows. So the request is refused instead, and the whole publish path
    keeps the closed-set property it is built on.

    **Why it is named.** The store validates every document it reads (`annotator.state.store`), so an
    unreadable ontology would otherwise surface as a bare `pydantic.ValidationError`: an anonymous 500 that
    names neither the project nor the reason and repeats on every call. Naming it turns "this project is
    bricked" into a sentence an operator can act on, and `fields` says which part of the document to rewrite.

    **Why 409 rather than 500.** The stored document conflicts with the code asked to read it —
    server state, not a server fault. A 5xx would tell the caller to retry, and a retry cannot fix
    yesterday's schema.

    **Reachable across a rolling upgrade that changes `LabelOntology`:** a replica on the old code writes a
    document the new code's models reject, and every replica on the new code then refuses it by name.
    """

    problem_type = "about:blank#unreadable-ontology"

    def __init__(self, kind: str, identifier: str, fields: Sequence[str]) -> None:
        self.kind = str(kind)
        self.identifier = str(identifier)
        #: Dotted paths into the stored document that failed validation — what to rewrite.
        self.fields: list[str] = [str(f) for f in fields]
        # The paths ride as an RFC 9457 §3.2 extension member rather than inside `detail`, so the
        # sentence stays stable while the structure a client could dispatch on stays machine-readable.
        super().__init__(
            f"annotation {self.kind} {self.identifier} carries a stored ontology this build cannot read",
            extensions={"kind": self.kind, "identifier": self.identifier, "fields": self.fields},
        )


#: Where the ontology sits inside a whole stored annotation document, and the prefix every seam
#: reports its failing paths under. Held here rather than spelled at each call site so a rename of
#: the field cannot leave one seam reporting a path that no longer exists.
ONTOLOGY_FIELD: Final = "ontology"


def refuse_unreadable_ontology(exc: PydanticValidationError, kind: str, identifier: str) -> NoReturn:
    """Re-raise `exc` as an :class:`UnreadableOntology`, or as ITSELF when the ontology is not why it failed.

    SCOPED ON PURPOSE: a whole stored document can fail for any of its fields, and only the ontology has the
    closed-set contract this refusal is named for, so a failure elsewhere stays the failure it is.

    Every path that failed is reported, not just the ontology's: the document has to be rewritten as
    a whole, and hiding the other paths would send the operator back for a second round.
    """
    located = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
    if not any(field == ONTOLOGY_FIELD or field.startswith(f"{ONTOLOGY_FIELD}.") for field in located):
        raise exc
    raise UnreadableOntology(kind, identifier, located) from exc


#: (from, event) -> (to, required permission). `None` = caused by the system (the publish saga, or a
#: lease running out), never by a principal, so there is no permission to check.
PROJECT_EDGES: Final[dict[tuple[ProjectState, str], tuple[ProjectState, str | None]]] = {
    (ProjectState.DRAFT, "open"): (ProjectState.LABELING, "can_manage"),
    (ProjectState.LABELING, "freeze"): (ProjectState.FROZEN, "can_manage"),
    (ProjectState.FROZEN, "open"): (ProjectState.LABELING, "can_manage"),
    (ProjectState.FROZEN, "publish"): (ProjectState.PUBLISHING, "can_publish"),
    (ProjectState.PUBLISH_FAILED, "publish"): (ProjectState.PUBLISHING, "can_publish"),
    (ProjectState.PUBLISHING, "publish_succeeded"): (ProjectState.PUBLISHED, None),
    (ProjectState.PUBLISHING, "publish_failed"): (ProjectState.PUBLISH_FAILED, None),
    (ProjectState.FROZEN, "archive"): (ProjectState.ARCHIVED, "can_manage"),
    (ProjectState.PUBLISHED, "archive"): (ProjectState.ARCHIVED, "can_manage"),
}

#: `send` leaves project state UNCHANGED and is legal only in draft/labeling. Sending into a frozen,
#: publishing, published or archived project is a 409 (§5.1) — modelled as absence, not a special case.
PROJECT_SEND_STATES: Final[frozenset[ProjectState]] = frozenset({ProjectState.DRAFT, ProjectState.LABELING})

TASK_EDGES: Final[dict[tuple[TaskState, str], tuple[TaskState, str | None]]] = {
    (TaskState.UNASSIGNED, "claim"): (TaskState.CLAIMED, "can_claim"),
    (TaskState.CHANGES_REQUESTED, "claim"): (TaskState.CLAIMED, "can_claim"),
    (TaskState.UNASSIGNED, "assign"): (TaskState.CLAIMED, "can_manage"),
    (TaskState.CHANGES_REQUESTED, "assign"): (TaskState.CLAIMED, "can_manage"),
    # save_draft is a self-edge: it renews the lease without changing state.
    (TaskState.CLAIMED, "save_draft"): (TaskState.CLAIMED, "can_annotate"),
    (TaskState.CLAIMED, "submit"): (TaskState.IN_REVIEW, "can_annotate"),
    (TaskState.CLAIMED, "release"): (TaskState.UNASSIGNED, "can_annotate"),
    (TaskState.CLAIMED, "lease_expired"): (TaskState.UNASSIGNED, None),
    (TaskState.CLAIMED, "skip"): (TaskState.SKIPPED, "can_annotate"),
    (TaskState.IN_REVIEW, "accept"): (TaskState.ACCEPTED, "can_review"),
    (TaskState.IN_REVIEW, "fix_and_accept"): (TaskState.ACCEPTED, "can_review"),
    (TaskState.IN_REVIEW, "request_changes"): (TaskState.CHANGES_REQUESTED, "can_review"),
    # §5.2: `reopen` is a MANAGER action, not a reviewer one — it un-finishes work that was
    # already accepted, which is a scheduling decision rather than a review verdict.
    (TaskState.ACCEPTED, "reopen"): (TaskState.CHANGES_REQUESTED, "can_manage"),
    (TaskState.SKIPPED, "requeue"): (TaskState.UNASSIGNED, "can_manage"),
}

#: A reviewer may not accept their own submission (§5.2), checked against the task's `submitted_by`.
#: Naming the events here keeps the rule discoverable from the machine — and gives
#: :func:`identity_violation`, which both the API layer and the store apply, one place to read it.
SELF_REVIEW_FORBIDDEN: Final[frozenset[str]] = frozenset({"accept", "fix_and_accept", "request_changes"})

#: §5.2 rule 2 — "only the lease holder writes … even a manager". `skip` belongs here with
#: `save_draft` and `submit`: it is a decision ABOUT the work someone is holding, and letting a
#: passer-by make it discards another annotator's claim (and, with `requeue_for_others`, their turn).
#: A manager who wants the task must `release` it and re-`assign`.
LEASE_HOLDER_ONLY: Final[frozenset[str]] = frozenset({"save_draft", "submit", "skip"})

#: §5.2 — `release` is "lease holder OR can_manage". It is the documented escape hatch for a task
#: pinned to someone unavailable, so it is deliberately NOT in `LEASE_HOLDER_ONLY`:
#: :func:`identity_violation` allows the holder, or anyone the caller states holds `can_manage`.
HOLDER_OR_MANAGER: Final[frozenset[str]] = frozenset({"release"})

#: §5.2 rule 5 — "nothing escapes a published project". Once a project reaches one of these, every
#: task transition is rejected: provenance is frozen with the artifact, and a task that moved after
#: the publish would describe a dataset that no longer matches it.
FROZEN_PROJECT_STATES: Final[frozenset[ProjectState]] = frozenset({ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED})


#: The task states whose shapes take edits, and the relation each asks of the editor: the claim's holder annotates, and
#: a reviewer may fix the work under review before accepting it (`fix_and_accept`). Every other state is finished or not
#: yet started, so an edit there would write shapes no annotator or reviewer is looking at.
EDIT_PERMISSION: Final[dict[TaskState, str]] = {TaskState.CLAIMED: "can_annotate", TaskState.IN_REVIEW: "can_review"}


#: Why a person other than a claim's holder is refused an edit or a holder's event: the holder is a principal, kept out
#: of the words a person reads (the HTTP layer names them in the problem's machine fields).
HELD_BY_ANOTHER: Final = "another person holds this item"


def edit_violation(*, state: TaskState, subject: str, assignee: str | None, submitted_by: str | None) -> str | None:
    """Why `subject` may not edit a task's shapes in `state`, or `None`.

    The identity half of an edit, beside the relation `EDIT_PERMISSION` names: a claimed task is edited by its holder
    alone (§5.2 rule 2, "even a manager"), and a task in review by a reviewer who did not submit it (§5.2's self-review
    rule). Pure, like `identity_violation`, so the HTTP layer and the store apply the one rule.
    """
    if state is TaskState.CLAIMED and assignee != subject:
        return HELD_BY_ANOTHER
    if state is TaskState.IN_REVIEW and submitted_by == subject:
        return "a reviewer may not edit their own submission"
    return None


def identity_violation(
    *,
    event: str,
    subject: str | None,
    assignee: str | None,
    submitted_by: str | None,
    subject_can_manage: bool = False,
) -> str | None:
    """The three IDENTITY-BOUND rules of §5.2, as a pure function of the task's own identity rows.

    They are not edges and cannot live in `TASK_EDGES`: they depend on WHO is firing and on columns
    the table does not carry. They live here, next to the sets that name them, because two layers apply
    them: the HTTP layer against its snapshot of the task (that is where a clean 403 comes from) and the
    store inside the transaction holding the task's row lock (that is where the answer is true). Two
    implementations would drift, and the one that drifted would be the one that decided.

    `subject_can_manage` is a VERIFIED INPUT, not a check performed here. `release` is "lease holder
    OR can_manage", and `can_manage` is the checker's answer: the caller resolves it and states it, which
    is what keeps this module testable with no store and no catalog.

    Returns the reason the transition is refused, or `None` when nothing here objects.
    """
    if event in SELF_REVIEW_FORBIDDEN and submitted_by is not None and submitted_by == subject:
        return "a reviewer may not review their own submission"
    if event in LEASE_HOLDER_ONLY and assignee not in (None, subject):
        # §5.2 rule 2 — "even a manager". A manager who wants the task must `release` and re-`assign`.
        return HELD_BY_ANOTHER
    if event in HOLDER_OR_MANAGER and assignee not in (None, subject) and not subject_can_manage:
        return f"{HELD_BY_ANOTHER} — releasing it needs can_manage"
    return None


class Refusal(BaseModel):
    """Why one person may not fire one event on one task now: which rule refused it, and the sentence a person reads."""

    #: `system`: no principal fires it; `illegal`: no such edge from the state; `permission`: the caller lacks it;
    #: `frozen`: the project is past labeling; `identity`: a rule of who may (§5.2); `replica`: they hold, held or worked
    #: another replica of the item.
    kind: Literal["system", "illegal", "permission", "frozen", "identity", "replica"]
    reason: str
    #: The relation the caller lacks, for `permission`.
    permission: str | None = None


#: Why the caller may not take a replica of an item another replica of which they hold, held or worked.
ANOTHER_REPLICA: Final = "one replica per annotator per group: you hold, held or worked another replica of this item"


class CallerFacts(BaseModel):
    """What the rule of who may fire what reads of the caller, beside the task's own rows: the project permissions they
    hold, whether the project is past labeling, and another replica of the task's group they hold, held or worked
    (`AnnotatorStore.worked_replicas`)."""

    subject: str
    held: frozenset[str]
    #: The project's state when it is past labeling (`FROZEN_PROJECT_STATES`), else `None`.
    frozen: ProjectState | None = None
    #: A replica of the task's group, other than the task, the caller holds, held or worked, or `None`.
    other_replica: str | None = None


def event_refusal(event: str, task: Task, caller: CallerFacts) -> Refusal | None:
    """Why ``caller`` may not fire ``event`` on ``task`` as it reads now (a lapsed claim released), or `None`.

    THE ONE RULE of who may fire what: the task door refuses by it (`tasks.fire_task_event`) and every listing offers by
    it (`caller_events`), so what a page offers is what the door accepts. The store applies its identity and replica
    halves again inside the transaction holding the task's row lock, which is what decides a race.

    ``assign`` is judged for the CALLER (a manager may distribute work): whether its RECIPIENT holds, held or worked
    another replica of the item is not known until they are named, and the store refuses that at the door, 409, per item.
    """
    try:
        _target, permission = task_transition(task.state, event)
    except IllegalTransition as exc:
        return Refusal(kind="illegal", reason=str(exc))
    if permission is None:
        return Refusal(kind="system", reason=f"{event} is caused by the system, not by a principal, and cannot be fired over HTTP")
    if permission not in caller.held:
        return Refusal(kind="permission", reason=f"you lack {permission} on this item", permission=permission)
    if caller.frozen is not None:
        return Refusal(kind="frozen", reason=f"this project is {caller.frozen} — {event} is rejected; provenance is frozen with the published artifact")
    violation = identity_violation(
        event=event, subject=caller.subject, assignee=task.assignee, submitted_by=task.submitted_by, subject_can_manage="can_manage" in caller.held
    )
    if violation is not None:
        return Refusal(kind="identity", reason=violation)
    if event == "claim" and caller.other_replica is not None:
        return Refusal(kind="replica", reason=ANOTHER_REPLICA)
    return None


def caller_events(task: Task, caller: CallerFacts) -> list[dict[str, str]]:
    """The edges out of the task's state ``caller`` may fire on it now: those `event_refusal` refuses nothing. What every
    surface offers a person, so none offers what the door would refuse."""
    return [event for event in legal_task_events(task.state) if event_refusal(event["event"], task, caller) is None]


def project_transition(state: ProjectState, event: str) -> tuple[ProjectState, str | None]:
    """Resolve a project edge, or raise `IllegalTransition`.

    `send` is special: legal in draft/labeling and a no-op on state, so it returns `state` unchanged.
    """
    if event == "send":
        if state not in PROJECT_SEND_STATES:
            raise IllegalTransition("project", state, event)
        return state, "can_send_items"
    try:
        return PROJECT_EDGES[(state, event)]
    except KeyError:
        raise IllegalTransition("project", state, event) from None


def task_transition(state: TaskState, event: str) -> tuple[TaskState, str | None]:
    """Resolve a task edge, or raise `IllegalTransition`."""
    try:
        return TASK_EDGES[(state, event)]
    except KeyError:
        raise IllegalTransition("task", state, event) from None


def legal_project_events(state: ProjectState) -> list[dict[str, str]]:
    """The PRINCIPAL-fireable edges out of `state`, each with the permission that gates it.

    This is what the API returns so the UI renders the transitions the backend supplies rather than
    hardcoding a second copy of the table that drifts. System edges (permission `None` — fired by
    the saga, never a principal) are excluded: a rendered control for one is a button that can only
    403. `send` is included where legal — it is an edge in every sense but the state change.
    """
    events = [
        {"event": event, "to": str(target), "permission": permission}
        for (from_state, event), (target, permission) in PROJECT_EDGES.items()
        if from_state is state and permission is not None
    ]
    if state in PROJECT_SEND_STATES:
        events.append({"event": "send", "to": str(state), "permission": "can_send_items"})
    return sorted(events, key=lambda e: e["event"])


def legal_task_events(state: TaskState) -> list[dict[str, str]]:
    """The principal-fireable edges out of `state` — the task half of `legal_project_events`."""
    return sorted(
        (
            {"event": event, "to": str(target), "permission": permission}
            for (from_state, event), (target, permission) in TASK_EDGES.items()
            if from_state is state and permission is not None
        ),
        key=lambda e: e["event"],
    )


def submit_target(review_required: bool) -> TaskState:
    """`submit` lands in `in_review`, or straight in `accepted` when the project waives review."""
    return TaskState.IN_REVIEW if review_required else TaskState.ACCEPTED


def may_publish(task_states: list[TaskState]) -> bool:
    """The publish precondition, mechanical (§5.1): EVERY task terminal. One `in_review` blocks it."""
    return all(s in {TaskState.ACCEPTED, TaskState.SKIPPED} for s in task_states)


def lapse_expired_claim(task: Task, now: datetime) -> bool:
    """Apply `lease_expired` to a claim whose lease ran out by `now`, and say whether it did.

    A lease is a holder and an expiry, and nothing fires at the expiry: a claim past it is released wherever
    the task is next read (`now` is the database's clock, the one every reader shares). The edge is recorded
    AT the expiry rather than at the read, so the trail says when the lease lapsed whoever noticed it, and a
    read that only shows it and the write that later persists it record the same transition. A pinned claim
    (`assign`, `lease_expires_at is None`) never lapses. The draft is untouched: an annotator whose lease
    lapsed has not lost work.
    """
    expires = task.lease_expires_at
    if task.state is not TaskState.CLAIMED or expires is None or expires > now:
        return False
    target, _ = task_transition(task.state, "lease_expired")
    task.transitions.append(Transition(at=expires, by="system", event="lease_expired", from_state=task.state, to_state=target))
    task.state, task.assignee, task.lease_expires_at = target, None, None
    return True
