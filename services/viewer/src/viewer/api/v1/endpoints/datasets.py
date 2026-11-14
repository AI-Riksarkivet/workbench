"""Dataset enumeration + descriptor endpoints — the multi-dataset front door.

``GET /api/datasets`` lists every ``warehouse$namespace`` the catalog shows the
caller (tables + available capabilities per dataset); ``GET /api/datasets/{id}/
descriptor`` hands the frontend the full merged descriptor it renders from
(LANCE_MEDIA_MERGE §4.2/§4.4). Datasets whose descriptor fails to load are
skipped from the listing (logged), never half-served.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from client_kit.lancekit.descriptor import DatasetDescriptor
from client_kit.lancekit.registry import DatasetRegistry, UnknownDatasetError
from client_kit.media.deps import StateDep
from client_kit.media.state import AppState, dataset_handle, ensure_registry
from service_kit.exceptions import ForbiddenError, NotFoundError
from viewer.api.security import AUTHENTICATED, CallerDep


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["datasets"])


class TableFacts(BaseModel):
    """The discovered shape of one table, as listed per dataset."""

    row_count: int
    version: int
    n_columns: int


class DatasetSummary(BaseModel):
    """One servable dataset: its tables and the capabilities that probe available."""

    id: str
    tables: dict[str, TableFacts]
    capabilities: list[str] = Field(default_factory=list)


class DatasetsResponse(BaseModel):
    datasets: list[DatasetSummary]


def _registry(state: AppState) -> DatasetRegistry:
    """The registry behind ``dataset_handle``, named here because enumeration
    has no dataset id to resolve through it. Construction lives ONLY in
    ``client_kit.media.state.ensure_registry`` — an unlocked copy of that
    lazy-init used to sit here and raced the guarded one (SK-06)."""
    return ensure_registry(state)


@router.get("/datasets", dependencies=[AUTHENTICATED])
async def list_datasets(caller: CallerDep, state: StateDep) -> DatasetsResponse:
    """The corpora the lakehouse shows this caller: what its catalog lists for them, on their own token.

    A corpus the caller may not read is not listed because the lakehouse does not list it, or refuses to open it; the
    viewer adds no gate of its own. A dataset whose descriptor fails to load is skipped, logged, never half-served.
    """

    # Off the loop: listing and opening are catalog calls and manifest reads, and this is the first call every zone
    # makes on page load.
    def _collect() -> list[DatasetSummary]:
        registry = _registry(state)
        collected: list[DatasetSummary] = []
        for dataset_id in registry.list_ids(caller):
            try:
                descriptor = registry.get(dataset_id, caller).descriptor
            except (UnknownDatasetError, ValueError, NotFoundError, ForbiddenError) as exc:
                logger.warning("skipping dataset %s: %s", dataset_id, exc)
                continue
            collected.append(
                DatasetSummary(
                    id=dataset_id,
                    tables={
                        name: TableFacts(row_count=info.row_count, version=info.version, n_columns=len(info.columns))
                        for name, info in descriptor.tables.items()
                    },
                    capabilities=[name for name in descriptor.declared.capabilities if descriptor.capability_available(name)],
                )
            )
        return collected

    return DatasetsResponse(datasets=await run_in_threadpool(_collect))


@router.get("/datasets/{dataset_id}/descriptor", dependencies=[AUTHENTICATED])
async def dataset_descriptor(caller: CallerDep, dataset_id: str, state: StateDep) -> DatasetDescriptor:
    """The full merged descriptor (discovered tables + declared roles), read as the caller; 404 via the domain handler
    for an unknown or descriptor-less id, and the lakehouse's own refusal for one it will not open for them."""
    handle = await run_in_threadpool(dataset_handle, state, dataset_id, caller)
    return handle.descriptor
