"""Search's settings: the media services' shared ones (``WORKBENCH_*``) and its own (``SEARCH_*``)."""

from functools import lru_cache

from pydantic import Field

from client_kit.media.config import GovernedMediaSettings, MediaSettings


class SearchSettings(GovernedMediaSettings):
    """The search service's config: the media plane's, the workbench's sign-in and store, and its client for lakehouse tokens.

    A lakehouse read as the person decides what they may search, on a token search exchanges or passes on for them; every
    other source is searched only by its declared readers, which the workbench's own OpenFGA decides
    (`GovernedMediaSettings`).
    """

    #: Version-keyed search result cache (the read-fast tier): how many result sets one process keeps; 0 disables it
    #: (then no per-request version reads are paid). A write to any table a query reads bumps its Lance version and
    #: strands the old entry (LRU-evicted). A hit row carries its display body, so a page of hits can run to megabytes:
    #: the byte ceiling below is the memory bound, the count only the lookup bound.
    cache_size: int = Field(default=256, ge=0, alias="SEARCH_CACHE_SIZE")
    #: Total bytes of cached results kept (approximated as serialized JSON length); 0 removes the byte bound.
    cache_bytes: int = Field(default=64 * 1024 * 1024, ge=0, alias="SEARCH_CACHE_BYTES")


def search_settings(settings: MediaSettings) -> SearchSettings:
    """``settings`` as search's own: the state search serves from is built from them (`search.main`)."""
    if not isinstance(settings, SearchSettings):
        raise TypeError(f"search serves from SearchSettings, not {type(settings).__name__}")
    return settings


@lru_cache
def get_search_settings() -> SearchSettings:
    return SearchSettings()
