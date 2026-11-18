"""Per-service settings — the shared data-plane config plus viewer-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (VIEWER_*) are prefixed.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field

from client_kit.media.config import GovernedMediaSettings


class ViewerSettings(GovernedMediaSettings):
    """The viewer's config: the media plane's, the workbench's sign-in and store, and its client for tokens to the lakehouse.

    A lakehouse read as the person decides what they may see, on a token the viewer exchanges or passes on for them; every
    other source is read only by its declared readers, which the workbench's own OpenFGA decides (`GovernedMediaSettings`).
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
    #: How many views set up on a table nobody stamped are built at once (`viewer.services.build_slots`): each is bounded
    #: (`viewer.services.derived_views`; the heaviest measured at about 330 MB on a 3,000,000-row table), and a build
    #: past this many is refused with 503 and Retry-After. The chart sets it from `media.viewer.derivedBuilds`.
    derived_builds: int = Field(default=2, ge=1, le=8, alias="VIEWER_DERIVED_BUILDS")
    #: How long a signed media URL serves its object.
    media_url_ttl_seconds: int = Field(default=300, ge=10, le=3600, alias="VIEWER_MEDIA_URL_TTL_SECONDS")


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
