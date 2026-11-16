"""Typed application settings (pydantic-settings).

Read once via :func:`get_settings`; routers read it off ``state.settings`` (the
injected :class:`~client_kit.media.state.AppState`). Only env-varying values live here —
algorithmic constants (RRF k, probe tokens, column-exclude sets) stay as module
constants in their feature packages.

Env vars are ``MEDIA_*`` (see aliases). ``cors_origins`` accepts either a JSON
list or a bare comma-separated string (``MEDIA_CORS_ORIGINS=https://a,https://b``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from client_kit.sources import Source, SourceIdentity, Sources


class MediaSettings(BaseSettings):
    """The MEDIA plane's settings (``MEDIA_*``) — the config of the services built on the media library.

    NAMED `MediaSettings`, and it used to be a second class called `Settings`. One distribution held
    two of them: this one on `MEDIA_*` aliases, and `service_kit.config.Settings` on `WORKBENCH_*` with an
    `env_prefix`. They are not related by inheritance and share almost no field, so which one a
    `settings: Settings` annotation, a `SettingsDep` or a stack frame referred to depended entirely on
    which module the reader had open. `Settings` survives below as a back-compat alias for the
    services that still import it by that name.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Env vars are MEDIA_* (§4.4). Pre-rename env aliases were dropped in the
    # lance-media rename.
    embed_url: str = Field(default="http://127.0.0.1:8001", alias="MEDIA_EMBED_URL")
    rerank_url: str = Field(default="http://127.0.0.1:8002", alias="MEDIA_RERANK_URL")
    host: str = Field(default="127.0.0.1", alias="MEDIA_HOST")
    descriptor_dir: Path = Field(default=Path("config/descriptors"), alias="MEDIA_DESCRIPTOR_DIR")
    cors_origins: list[str] = Field(default_factory=lambda: ["*"], alias="MEDIA_CORS_ORIGINS")

    # Version-keyed search result cache (the read-fast tier). Number of result
    # sets to retain per app instance; 0 disables it entirely (then no per-request
    # version reads are paid). A write to any table a query reads bumps its Lance
    # version and strands the old entry (LRU-evicted). Entries are NOT uniformly
    # small — a hit row carries display.body (transcript text), so a full page of
    # hits can run to megabytes; the byte ceiling below is the real memory bound,
    # the entry count is just the lookup bound (#141).
    search_cache_size: int = Field(default=256, ge=0, alias="MEDIA_SEARCH_CACHE_SIZE")
    # Total bytes of cached results to retain (approximated as serialized JSON
    # length). 0 removes the byte bound (count-only, the pre-#141 behaviour).
    search_cache_bytes: int = Field(default=64 * 1024 * 1024, ge=0, alias="MEDIA_SEARCH_CACHE_BYTES")
    #: The MEMORY ceiling on the atlas `/points` cache. Its twin above has had one since someone
    #: measured the problem; this one evicted on entry count alone (12) while its own comment said
    #: "each is multi-MB". Three declared spaces across four corpora is already twelve keys, and at
    #: ~100 MB per Arrow payload that is 1.2 GB resident in a one-replica pod. `0` disables the byte
    #: bound only, exactly as above — the two must not diverge on the meaning of their settings.
    points_cache_bytes: int = Field(default=256 * 1024 * 1024, ge=0, alias="MEDIA_POINTS_CACHE_BYTES")
    #: The request-body ceiling for the media apps.
    #:
    #: These are the apps that accept MULTIPART UPLOADS, and they were the ones with no cap: they
    #: build through `client_kit.media.middleware`, not the fleet factory that grew one. A file part
    #: is spooled to a SpooledTemporaryFile in FULL before the handler is entered, so the handler's
    #: own `read(cap + 1)` bounds its memory and cannot bound the landing zone.
    #:
    #: 32 MiB, not the catalog's 256: that ceiling exists for bulk Arrow-IPC table writes, and reusing
    #: it here would make the bound meaningless for a voice snippet. It stays comfortably ABOVE
    #: `_MAX_UPLOAD_BYTES` so the handler's own 400 — which names the limit in the caller's terms —
    #: is still the message an oversize upload gets, rather than a bare 413 from the door.
    max_body_bytes: int = Field(default=32 * 1024 * 1024, ge=0, alias="MEDIA_MAX_BODY_BYTES")

    #: Whether this app serves `/docs`, `/redoc` and `/openapi.json`. OFF by default.
    #:
    #: A media app built with none of the three set would serve all three, FastAPI's defaults. See
    #: `service_kit.config.Settings.docs_enabled` for why the default is closed.
    docs_enabled: bool = Field(default=False, alias="MEDIA_DOCS")

    #: The sources the media plane reads (`client_kit.sources`): the declaration git holds, as the file the deployment
    #: mounts (``WORKBENCH_SOURCES_FILE``) or, for a test or a one-off run, its ``sources`` list inline
    #: (``WORKBENCH_SOURCES``, JSON). Exactly one of the two.
    sources_file: Path | None = Field(default=None, alias="WORKBENCH_SOURCES_FILE")
    sources_inline: list[Source] | None = Field(default=None, alias="WORKBENCH_SOURCES")
    #: The dataset a request that names none reads, written ``<source>~<namespace>``; unset, such a request is a 404.
    default_dataset: str | None = Field(default=None, alias="MEDIA_DEFAULT_DATASET")

    _sources: Sources = PrivateAttr()

    @model_validator(mode="after")
    def _read_sources(self) -> Self:
        """The declaration, read and checked at load: a deployment that names no source, names it twice, or names a default
        dataset no source could hold fails here rather than at its first request."""
        if (self.sources_file is None) == (self.sources_inline is None):
            raise ValueError("set exactly one of WORKBENCH_SOURCES_FILE (the declaration's file) and WORKBENCH_SOURCES (its sources, inline)")
        self._sources = Sources.read(self.sources_file) if self.sources_file is not None else Sources(items=list(self.sources_inline or []))
        if self.default_dataset is not None:
            self._sources.dataset(self.default_dataset)
        return self

    @property
    def sources(self) -> Sources:
        return self._sources

    @property
    def reads_lakehouse(self) -> bool:
        """Whether a lakehouse catalog is read over its REST door, which authenticates every caller: the workbench's own
        reads and writes there need its client."""
        lakehouse = self._sources.lakehouse
        return lakehouse is not None and lakehouse.uri is not None

    @property
    def reads_lakehouse_as_person(self) -> bool:
        """Whether that lakehouse is read as the person (``identity: person``): a person's read then needs a way to present
        their token. A lakehouse read as the workbench needs none of theirs."""
        lakehouse = self._sources.lakehouse
        return lakehouse is not None and lakehouse.uri is not None and lakehouse.identity is SourceIdentity.PERSON

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_media_settings() -> MediaSettings:
    """The PROCESS-WIDE media settings, read once — for a service `main` that wants one object for
    the app's whole life.

    NAMED APART from `service_kit.dependencies.get_settings`, and the two are not interchangeable:
    that one takes a `Request` and returns whatever the app's own lifespan bound, this one takes
    nothing and returns an lru_cached singleton. Two functions with one name and incompatible
    signatures is how `AppState` came to default every instance in the process to a single shared
    object — see `client_kit.media.state.AppState.settings`, which no longer does.
    """
    return MediaSettings()


#: ALIASES. The media services import these names (and subclass `Settings` in their own `core/config.py`).
#: They are aliases, not second definitions: `Settings is MediaSettings` and `get_settings is get_media_settings`.
Settings = MediaSettings
get_settings = get_media_settings
