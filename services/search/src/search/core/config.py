"""Per-service settings — the shared data-plane config plus search-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (SEARCH_*) are prefixed.
"""

from functools import lru_cache

from pydantic import Field

from client_kit.media.config import Settings
from service_kit.governed.settings import LakehouseTokenSettings, OidcSettings


class SearchSettings(Settings, OidcSettings, LakehouseTokenSettings):
    """The search service's config: the media plane's, the workbench's sign-in, and its client for lakehouse tokens.

    Search reads every corpus as the person asking, on a token it exchanges for them, so the lakehouse decides what they
    may search; it checks nothing of its own on a corpus.
    """

    service_name: str = "search"
    service_port: int = Field(default=8102, alias="SEARCH_PORT")


@lru_cache
def get_search_settings() -> SearchSettings:
    return SearchSettings()
