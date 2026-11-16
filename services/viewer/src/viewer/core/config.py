"""Per-service settings — the shared data-plane config plus viewer-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (VIEWER_*) are prefixed.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field

from client_kit.media.config import Settings
from service_kit.governed.settings import GovernedAuthSettings, LakehouseTokenSettings


class ViewerSettings(GovernedAuthSettings, LakehouseTokenSettings, Settings):
    """The viewer's config: the media plane's, the workbench's sign-in, and its client for tokens to the lakehouse.

    The viewer reads every corpus as the person asking, on a token it exchanges for them, so the lakehouse decides
    what they may see; it checks nothing of its own on a corpus.
    """

    service_name: str = "viewer"
    service_port: int = Field(default=8101, alias="VIEWER_PORT")

    #: Where the voiceprint runner's Ray Serve app answers. EMPTY DISABLES the upload form, and that
    #: is the honest default: the read plane no longer carries a speaker encoder, so an estate that
    #: has not deployed the runner cannot embed — and saying so at the door beats a 500 from a model
    #: that is not there. The Lance-anchored GET forms are unaffected; they run no encoder.
    #:
    #: A setting rather than a literal because a hard-coded endpoint does not survive multi-env
    #: deploys (`fastapi` references/anti-patterns.md).
    voiceprint_serve_url: str = Field(default="", alias="VIEWER_VOICEPRINT_SERVE_URL")

    #: The mounted file holding the key media URLs are signed with (`viewer.services.signed_media`), read on each use so
    #: a rotated key signs and verifies from the next request. Unset, the signing door answers 503.
    media_url_key_file: Path | None = Field(default=None, alias="VIEWER_MEDIA_URL_KEY_FILE")
    #: How long a signed media URL serves its object.
    media_url_ttl_seconds: int = Field(default=300, ge=10, le=3600, alias="VIEWER_MEDIA_URL_TTL_SECONDS")


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
