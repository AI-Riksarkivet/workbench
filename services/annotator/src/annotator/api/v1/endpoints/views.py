"""Saved views shared by their link (ADR 0022).

A person shares a saved view of a dataset (its arrangement, set-ups and search, as the app keeps them) and gets an id
the app makes a link of. Opening the link is a read of the view's dataset under the reader's own rights, resolved
exactly as every other read of it (`workbench.media.state.dataset_handle`), so a person who may not read the dataset is
refused as any read of it is refused: the link grants nothing. A shared view is listed only to its owner (listing an
organisation's shared views is a later decision), and its owner can stop sharing it, after which the link opens nothing.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from annotator.api.dependencies import StoreDep
from annotator.api.security import CallerDep, CurrentSubject
from annotator.audit_actions import AnnotatorAuditAction
from annotator.people import RECORDS_PERSON
from annotator.projects.models import new_id
from annotator.state.store import SharedView
from annotator.text import Text
from workbench.exceptions import NotFoundError
from workbench.governed.audit import AuditOutcome, audit
from workbench.governed.token_exchange import LakehouseCaller
from workbench.media.deps import StateDep
from workbench.media.state import AppState, dataset_handle


router = APIRouter(prefix="/views", tags=["views"], dependencies=RECORDS_PERSON)

#: The most a view's snapshot may hold, serialized: an arrangement, a few set-ups and a search are a few KiB.
MAX_SNAPSHOT_BYTES: Final = 64 * 1024
#: The most views one listing answers.
MAX_LISTED: Final = 200

DatasetId = Annotated[str, Field(min_length=1, max_length=512)]
ViewId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class ShareRequest(BaseModel):
    dataset: DatasetId
    name: Annotated[Text, Field(min_length=1, max_length=200)]
    #: What the app keeps for the view, applied by the app as it checks a view from its own storage.
    snapshot: dict[str, Any]

    @field_validator("snapshot")
    @classmethod
    def _bounded(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        size = len(json.dumps(snapshot, separators=(",", ":")).encode())
        if size > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"a view's snapshot holds at most {MAX_SNAPSHOT_BYTES} bytes; this one holds {size}")
        return snapshot


class SharedViews(BaseModel):
    views: list[SharedView]
    #: Whether the caller shares more views of the dataset than the newest `MAX_LISTED` listed.
    more: bool


async def _read(state: AppState, dataset: str, caller: LakehouseCaller) -> None:
    """Read ``dataset`` as ``caller``, as every read of it is made; its refusal, or its absence, is the answer."""
    await run_in_threadpool(dataset_handle, state, dataset, caller)


@router.post("", status_code=status.HTTP_201_CREATED)
async def share_view(payload: ShareRequest, caller: CallerDep, state: StateDep, store: StoreDep, subject: CurrentSubject) -> SharedView:
    """Share a saved view of a dataset the caller may read; 403 or 404 as a read of that dataset answers."""
    await _read(state, payload.dataset, caller)
    view = await store.share_view(SharedView(view_id=new_id(), owner=subject, dataset=payload.dataset, name=payload.name, snapshot=payload.snapshot))
    audit(AnnotatorAuditAction.VIEW_SHARE, AuditOutcome.SUCCESS, subject=subject, resource=f"view:{view.view_id}", dataset=view.dataset)
    return view


@router.get("")
async def views_i_share(dataset: Annotated[str, Query(min_length=1, max_length=512)], store: StoreDep, subject: CurrentSubject) -> SharedViews:
    """The views the caller shares of ``dataset``, newest first, at most `MAX_LISTED`, and whether there are more."""
    views = await store.views_shared_by(subject, dataset, limit=MAX_LISTED + 1)
    return SharedViews(views=views[:MAX_LISTED], more=len(views) > MAX_LISTED)


@router.get("/{view_id}")
async def open_view(view_id: ViewId, caller: CallerDep, state: StateDep, store: StoreDep) -> SharedView:
    """The shared view ``view_id``: 404 when nobody shares one by that id, then 403 or 404 as the caller's read of its
    dataset answers."""
    view = await store.shared_view(view_id)
    if view is None:
        raise NotFoundError(f"nobody shares a view {view_id!r}: its owner may have stopped sharing it")
    await _read(state, view.dataset, caller)
    return view


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_sharing(view_id: ViewId, store: StoreDep, subject: CurrentSubject) -> None:
    """Stop sharing a view the caller shares, after which its link opens nothing; 404 for any other."""
    if not await store.unshare_view(view_id, subject):
        raise NotFoundError(f"you share no view {view_id!r}")
    audit(AnnotatorAuditAction.VIEW_UNSHARE, AuditOutcome.SUCCESS, subject=subject, resource=f"view:{view_id}")
