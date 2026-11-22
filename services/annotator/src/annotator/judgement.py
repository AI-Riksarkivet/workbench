"""A judge's verdict on one submission, and the ask it comes from (ADR 0026).

A judge is a model the project names, reached by the workbench's model protocol (`annotator.model_endpoint`), that says
what it disagrees with among the labels a person submitted. Its verdict orders and marks the review list and decides
nothing: it is a row of its own (`judgements`, `annotator.state.store`), written beside the task and never to it, so no
item moves because of a judge. A verdict belongs to one submission (the task and the instant it was submitted): a
resubmitted item has none until it is judged again, and each submission's verdict is kept with what its reviewer then
decided.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, Field

from annotator.model_endpoint import protocol
from annotator.projects.models import Draft, ReviewAction, labels_of
from workbench.exceptions import ConflictError


NO_JUDGE: Final = "this project names no judge"


class NoJudge(ConflictError):
    """The project names no judge: nothing can be asked until its settings name one."""

    problem_type = "about:blank#no-judge"

    def __init__(self, project_id: str) -> None:
        super().__init__(NO_JUDGE, extensions={"project": project_id})


class Flag(BaseModel):
    """One thing the judge disagreed with: a shape of the submission, or the item as a whole (``shape_id`` absent)."""

    shape_id: str | None = None
    reason: str
    suggested_label: str = ""
    score: float


class Verdict(BaseModel):
    """What a judge said of one submission, as the door hands it to the store."""

    task_id: str
    #: The submission judged: the instant the task was submitted.
    submitted_at: datetime
    #: The revision of the item's work the judge read.
    revision: int
    model: str
    version: str
    #: The person whose request asked the judge.
    asked_by: str
    flags: list[Flag] = Field(default_factory=list)

    @property
    def flagged(self) -> bool:
        """Whether the judge disagreed with anything."""
        return bool(self.flags)


class Judgement(Verdict):
    """A verdict as it is kept: when it was recorded, on the database's clock, and what its reviewer then decided."""

    judged_at: datetime
    #: The reviewer's decision on the submission, once made: who, when and which.
    settled_by: str | None = None
    settled_at: datetime | None = None
    settled_action: ReviewAction | None = None
    #: Whether the reviewer agreed with the flags, for a flagged submission: what they said, else what their decision
    #: says (accepting the work as it is disagrees; fixing it or sending it back agrees). None on an unflagged one.
    agreed: bool | None = None


def agreed_with(event: str, said: bool | None) -> bool:
    """Whether a reviewer who fired ``event`` on a flagged submission agreed with its flags: what they ``said``, else
    what the decision says."""
    return said if said is not None else event != "accept"


def labels_asked(draft: Draft | None) -> list[protocol.AssessedLabel]:
    """The submission as a judge reads it: the item's labels (the shapes a publish would write), each under its shape id."""
    shapes, _links = labels_of(draft)
    return [
        protocol.AssessedLabel(
            id=shape.shape_id,
            shape_type=shape.shape_type,
            x=shape.x or 0.0,
            y=shape.y or 0.0,
            width=max(shape.width or 0.0, 0.0),
            height=max(shape.height or 0.0, 0.0),
            polygon=shape.polygon,
            label=shape.label or "",
            text=shape.text or "",
            char_start=shape.char_start,
            char_end=shape.char_end,
        )
        for shape in shapes
    ]


__all__ = ["NO_JUDGE", "Flag", "Judgement", "NoJudge", "Verdict", "agreed_with", "labels_asked"]
