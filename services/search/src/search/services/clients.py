"""Lazy vLLM query-encoder accessors bound to :class:`~workbench.media.state.AppState`.

Each accessor returns the cached client if present, else constructs it on first use and
writes it back onto the same ``AppState`` (so subsequent requests reuse it),
mapping any failure to a ``ServiceUnavailableError`` (HTTP 503). The URLs come
from ``state.settings``; the embedding client's ``expected_dim`` comes from the
default dataset's semantic vector binding — the descriptor, not a constant.
The encoder imports are deferred *inside* each function: it keeps FTS-only
startup free of the multimodal deps (numpy/Pillow), and it's the seam tests
monkeypatch (the symbol must resolve at call time, not import time).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from search.services.spec import SearchMode
from workbench.exceptions import ServiceUnavailableError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.media.state import dataset_handle


if TYPE_CHECKING:
    from search.services.encoders.embedding import EmbeddingClient
    from search.services.encoders.reranker import VLLMReranker
    from workbench.media.state import AppState

logger = logging.getLogger(__name__)


def _default_embed_dim(state: AppState, caller: LakehouseCaller) -> int:
    """The query-vector dimension of the default dataset's semantic binding.

    Falls back to the first declared vector binding when no ``semantic`` mode
    exists (any binding shares the encoder's output space); raises when the
    dataset declares no vectors at all — constructing an embedder then is a
    caller bug surfaced as the same 503 the accessor maps everything to.
    """
    handle = dataset_handle(state, None, caller)
    search = handle.declared.search
    if search is None or not search.vectors:
        raise RuntimeError("default dataset declares no vector bindings")
    binding = search.vectors.get(SearchMode.SEMANTIC.value) or next(iter(search.vectors.values()))
    return binding.dim


def ensure_embedder(state: AppState, caller: LakehouseCaller) -> EmbeddingClient:
    """Return the app's embedding client, connecting on first use (503 on failure)."""
    if state.embedder is not None:
        # Only this accessor fills the slot, with an `EmbeddingClient`; the shared state types it by what it closes.
        return cast("EmbeddingClient", state.embedder)
    try:
        from search.services.encoders.embedding import VLLMEmbeddingClient

        # Constructed with the real kwargs, unconditionally, so a drifted signature fails here rather than dropping a
        # kwarg silently; a double that cannot take these fails loudly.
        embedder = VLLMEmbeddingClient(state.settings.embed_url, expected_dim=_default_embed_dim(state, caller))
        state.embedder = embedder
        return embedder
    except Exception as e:
        logger.exception("failed to initialize embedding client")
        raise ServiceUnavailableError("embedding service unavailable") from e


def ensure_reranker(state: AppState) -> VLLMReranker:
    """Return the app's reranker client, connecting on first use (503 on failure)."""
    if state.reranker is not None:
        # Only this accessor fills the slot, with a `VLLMReranker`; the shared state types it by what it closes.
        return cast("VLLMReranker", state.reranker)
    try:
        from search.services.encoders.reranker import VLLMReranker

        reranker = VLLMReranker(state.settings.rerank_url)
        state.reranker = reranker
        return reranker
    except Exception as e:
        logger.exception("failed to initialize reranker client")
        raise ServiceUnavailableError("rerank service unavailable") from e
