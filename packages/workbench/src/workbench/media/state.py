"""Shared, app-wide resources built in lifespan and read via DI.

``AppState`` lives on ``app.state.resources`` and is reached through the ``StateDep`` dependency — routers never touch
``app.state`` directly. Data resolution goes through :func:`dataset_handle` (the multi-dataset registry), which the
state opens when it is built, before the service reports ready.

What stays unbuilt until first use, and why: a dataset's handle is the CALLER's (opened on their token, cached for them
alone), so no handle can be opened before a caller asks; the model clients (``embedder``, ``reranker``) belong to model
servers a deployment may not run, and the embedder's query dimension is read from the caller's dataset.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from workbench.lancekit.registry import DatasetRegistry
from workbench.media.config import MediaSettings


if TYPE_CHECKING:
    from workbench.governed.token_exchange import LakehouseCaller
    from workbench.lancekit.registry import DatasetHandle

logger = logging.getLogger(__name__)


@runtime_checkable
class ModelClient(Protocol):
    """A model server's client as the media plane holds it: what the shared lifespan calls on it at shutdown.

    Search fills the slots with its own clients (`search.services.clients`) and reads them as their own types.
    """

    def close(self) -> None: ...


class AppState(BaseModel):
    """Per-app resource handles + lazy vLLM client cache.

    The Lance/lancedb handles aren't Pydantic-validatable, hence
    ``arbitrary_types_allowed``. ``embedder``/``reranker`` are mutable slots that
    :mod:`search.services.clients` fills on first use and reuses thereafter. ``settings``
    carries the typed app config, read fresh per state.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Required, per state: there is no default catalog to read, and a default built from the process environment would
    # bind every state in the process to one configuration. A service passes its own (`make_media_state`).
    settings: MediaSettings
    #: The dataset registry, the one resolution path, built with the state from its settings: building it opens no source.
    registry: DatasetRegistry = Field(default_factory=lambda fields: DatasetRegistry(fields["settings"]))
    embedder: ModelClient | None = None
    reranker: ModelClient | None = None
    # One HTTP connection pool per process (health pings) — never per-request.
    http: httpx.Client | None = None
    # Memoized /points payloads keyed on (space, dataset version) — per-app, not
    # module-global, so two app instances (e.g. tests) can't cross-contaminate.
    # Writes are idempotent (same input → same bytes), so a plain dict suffices.
    # Carries the payload's SIZE alongside it, like `search_cache` below, so eviction can honour a
    # byte ceiling without re-measuring every entry. The atlas payload is `bytes`, so the size is
    # EXACT rather than the estimate the search cache has to make.
    points_cache: dict[tuple[str, str, int], tuple[bytes, int]] = Field(default_factory=dict)
    # The build of each payload in flight, so callers asking for the same cold payload at once await one build
    # (viewer.api.v1.endpoints.atlas); only the app's event loop touches it and `points_cache`.
    points_flights: dict[tuple[str, str, int], Any] = Field(default_factory=dict)
    # The Atlas, Graph and Tree a person set up on a table nobody stamped (viewer.api.v1.endpoints.derived_views), keyed
    # (dataset, table, version, read, the columns picked), each with its size; bounded and single-flight as the points.
    derived_cache: dict[tuple[str, str, int, str, str], tuple[bytes, int]] = Field(default_factory=dict)
    derived_flights: dict[tuple[str, str, int, str, str], Any] = Field(default_factory=dict)
    # The slots those builds take (viewer.services.build_slots.BuildSlots), made on the first build from the viewer's
    # settings: a count of builds under way, which opens nothing; only the app's event loop touches it.
    derived_slots: Any | None = None
    # The viewer's own opens of the tables its signed media URLs name (viewer.services.signed_media.ServiceReads), built
    # with the viewer's state (`viewer.main.create_viewer_state`).
    service_reads: Any | None = None
    # Version-keyed search result cache (search.services.result_cache): keyed
    # (dataset, table-version signature, query hash) → (the query's hits, their
    # approximate byte size — carried so eviction can honour the byte ceiling
    # without re-serializing every entry). Same per-app, version-invalidated
    # discipline as points_cache; bounded by search's cache size and bytes settings.
    search_cache: dict[tuple[str, str, str], tuple[list[Any], int]] = Field(default_factory=dict)
    # The search routes run in the threadpool, so the cache is read, moved and evicted from several threads at once: each
    # of those is done holding this lock (search.services.result_cache.run_cached), never the search itself.
    search_cache_lock: Any = Field(default_factory=threading.Lock)


def dataset_handle(state: AppState, dataset_id: str | None, caller: LakehouseCaller) -> DatasetHandle:
    """Resolve a dataset by id, as the caller, through the registry (``None`` is the configured default).

    ``caller`` is the person the catalog reads the dataset for, on their own token; the handle is theirs alone.
    The one resolution path every media route shares; an unknown id or an invalid descriptor is a ``NotFoundError``, so
    the RFC 9457 handler renders a clean 404.
    """
    from workbench.exceptions import NotFoundError
    from workbench.lancekit.registry import UnknownDatasetError

    try:
        return state.registry.get(dataset_id, caller) if dataset_id else state.registry.default(caller)
    except UnknownDatasetError as exc:
        raise NotFoundError(str(exc)) from exc
    except ValueError as exc:  # invalid/missing descriptor — a config problem, not a 500
        raise NotFoundError(str(exc)) from exc
