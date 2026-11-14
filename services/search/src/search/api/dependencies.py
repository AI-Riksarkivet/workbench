"""Search-group dependency wrappers — the seam between the app and the router.

The search group owns its own client deps (per §4.4 each router group carries
its own state deps); only :data:`~client_kit.media.deps.StateDep` is shared, since the
per-app :class:`~client_kit.media.state.AppState` is the one composition-level resource.
The embedder/reranker deps hand back *zero-arg getters* (bound to that state)
so the search service keeps its ``Callable[[], Client]`` signature and connects
lazily on first use. Tests can override any of these via
``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from client_kit.media.deps import StateDep
from search.services.clients import ensure_embedder, ensure_reranker
from service_kit.governed.deps import RawBearerToken


if TYPE_CHECKING:
    from search.services.encoders.embedding import EmbeddingClient
    from search.services.encoders.reranker import VLLMReranker

__all__ = ["EmbedderFactoryDep", "RerankerFactoryDep", "StateDep", "get_embedder", "get_reranker"]


def get_embedder(state: StateDep, caller_token: RawBearerToken = None) -> Callable[[], EmbeddingClient]:
    """A zero-arg getter that lazily connects (then caches) the embedding client; its dimension is read off the default
    dataset as the caller."""
    return lambda: ensure_embedder(state, caller_token)


def get_reranker(state: StateDep) -> Callable[[], VLLMReranker]:
    """A zero-arg getter that lazily connects (then caches) the reranker client."""
    return lambda: ensure_reranker(state)


EmbedderFactoryDep = Annotated[Callable[[], "EmbeddingClient"], Depends(get_embedder)]
RerankerFactoryDep = Annotated[Callable[[], "VLLMReranker"], Depends(get_reranker)]
