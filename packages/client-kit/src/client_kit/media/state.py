"""Shared, app-wide resources built in lifespan and read via DI.

``AppState`` lives on ``app.state.resources`` and is reached through the
``StateDep`` dependency — routers never touch ``app.state`` directly. Data
resolution goes through :func:`dataset_handle` (the multi-dataset registry);
the encoder/voice slots stay ``None`` until first use so an FTS-only
deployment never connects to a GPU server.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from client_kit.media.config import MediaSettings


if TYPE_CHECKING:
    from client_kit.lancekit.registry import DatasetHandle, DatasetRegistry
    from service_kit.governed.token_exchange import LakehouseCaller

logger = logging.getLogger(__name__)


class AppState(BaseModel):
    """Per-app resource handles + lazy vLLM client cache.

    The Lance/lancedb handles aren't Pydantic-validatable, hence
    ``arbitrary_types_allowed``. ``embedder``/``reranker`` are mutable slots that
    :mod:`search.services.clients` fills on first use and reuses thereafter. ``settings``
    carries the typed app config (vLLM URLs, host/port, CORS), read fresh per state.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Required, per state: there is no default catalog to read, and a default built from the process environment would
    # bind every state in the process to one configuration. A service passes its own (`make_media_state`).
    settings: MediaSettings
    embedder: Any | None = None  # search.services.encoders.embedding.VLLMEmbeddingClient
    reranker: Any | None = None  # search.services.encoders.reranker.VLLMReranker
    # Lazy in-process voice encoder for the upload voice search (CPU by design;
    # the GPUs belong to the vLLM servers). None until the first upload; the
    # lock guards first-load — sync handlers run in a threadpool, so two
    # concurrent first uploads would otherwise both construct the model.
    voice_encoder: Any | None = None  # viewer.services.wespeaker.VoiceEncoder
    voice_encoder_lock: Any = Field(default_factory=threading.Lock)
    # One HTTP connection pool per process (health pings) — never per-request.
    http: Any | None = None  # httpx.Client
    # Memoized /points payloads keyed on (space, dataset version) — per-app, not
    # module-global, so two app instances (e.g. tests) can't cross-contaminate.
    # Writes are idempotent (same input → same bytes), so a plain dict suffices.
    # Carries the payload's SIZE alongside it, like `search_cache` below, so eviction can honour a
    # byte ceiling without re-measuring every entry. The atlas payload is `bytes`, so the size is
    # EXACT rather than the estimate the search cache has to make.
    points_cache: dict[tuple[str, str, int], tuple[bytes, int]] = Field(default_factory=dict)
    # Version-keyed search result cache (search.services.result_cache): keyed
    # (dataset, table-version signature, query hash) → (the query's hits, their
    # approximate byte size — carried so eviction can honour the byte ceiling
    # without re-serializing every entry). Same per-app, version-invalidated
    # discipline as points_cache; bounded by settings.search_cache_size AND
    # settings.search_cache_bytes (0 = off / no byte bound respectively).
    search_cache: dict[tuple[str, str, str], tuple[list[Any], int]] = Field(default_factory=dict)
    # The dataset registry, the one resolution path. Same first-build hazard as voice_encoder: sync handlers run in a
    # threadpool, so two concurrent first requests would otherwise both construct the registry.
    registry: Any | None = None  # client_kit.lancekit.registry.DatasetRegistry
    registry_lock: Any = Field(default_factory=threading.Lock)


def ensure_registry(state: AppState) -> DatasetRegistry:
    """The ONE lazy first-build of ``state.registry`` (SK-06).

    Every path that needs the registry (id resolution via :func:`dataset_handle`, the viewer's enumeration, which has no
    dataset id to resolve through) goes through here, so the guarded construction exists exactly once.
    """
    # Imported per call, not at module top: the media plane stays importable without the Lance stack until a registry
    # is actually needed.
    from client_kit.lancekit.registry import DatasetRegistry

    if state.registry is None:
        # Double-checked so the steady state never takes the lock; without it two concurrent first requests both
        # construct, and the loser's registry (with whatever it already opened) is silently discarded.
        with state.registry_lock:
            if state.registry is None:
                state.registry = DatasetRegistry(state.settings)
    registry: DatasetRegistry = state.registry
    return registry


def dataset_handle(state: AppState, dataset_id: str | None, caller: LakehouseCaller) -> DatasetHandle:
    """Resolve a dataset by id, as the caller, through the registry (``None`` is the configured default).

    ``caller`` is the person the catalog reads the dataset for, on their own token; the handle is theirs alone.
    The one resolution path every media route shares; an unknown id or an invalid descriptor is a ``NotFoundError``, so
    the RFC 9457 handler renders a clean 404.
    """
    from client_kit.lancekit.registry import UnknownDatasetError
    from service_kit.exceptions import NotFoundError

    registry = ensure_registry(state)
    try:
        return registry.get(dataset_id, caller) if dataset_id else registry.default(caller)
    except UnknownDatasetError as exc:
        raise NotFoundError(str(exc)) from exc
    except ValueError as exc:  # invalid/missing descriptor — a config problem, not a 500
        raise NotFoundError(str(exc)) from exc
