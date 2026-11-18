"""Per-service settings — the shared data-plane config plus search-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (SEARCH_*) are prefixed.
"""

from functools import lru_cache

from pydantic import Field

from client_kit.media.config import GovernedMediaSettings


class SearchSettings(GovernedMediaSettings):
    """The search service's config: the media plane's, the workbench's sign-in and store, and its client for lakehouse tokens.

    A lakehouse read as the person decides what they may search, on a token search exchanges or passes on for them; every
    other source is searched only by its declared readers, which the workbench's own OpenFGA decides
    (`GovernedMediaSettings`).
    """

    service_name: str = "search"
    service_port: int = Field(default=8102, alias="SEARCH_PORT")


@lru_cache
def get_search_settings() -> SearchSettings:
    return SearchSettings()
