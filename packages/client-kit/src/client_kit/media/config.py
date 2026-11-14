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

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.naming import CATALOG_DELIMITER


def _warehouse_qualified(namespace: list[str], delimiter: str) -> None:
    """Refuse a namespace id with fewer than two segments or an empty one.

    Raises:
        ValueError: The namespace is not warehouse-qualified.
    """
    if len(namespace) < 2 or not all(namespace):
        raise ValueError(f"catalog namespace {delimiter.join(namespace)!r} is not warehouse-qualified: write it as <warehouse>{delimiter}<namespace>")


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

    #: Where the media plane finds its datasets: a lance-namespace catalog, exactly one of the two. A dataset is a
    #: namespace written ``warehouse$namespace``, its tables are what ListTables answers there, and each opens through
    #: the namespace on the storage options DescribeTable returns, so this process holds no store credential.
    #:
    #: ``MEDIA_CATALOG_URI`` is the catalog's REST door (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md`),
    #: reached as the caller on the caller's own bearer. ``MEDIA_CATALOG_DIR`` is lance-namespace's stock directory
    #: catalog under a local root (`lance_docs/ns_catalog/catalog/dir/index.md`), for development and the offline suite:
    #: the same code runs on it, with nobody to authenticate.
    catalog_uri: str | None = Field(default=None, alias="MEDIA_CATALOG_URI")
    catalog_dir: Path | None = Field(default=None, alias="MEDIA_CATALOG_DIR")
    catalog_delimiter: str = Field(default=CATALOG_DELIMITER, alias="MEDIA_CATALOG_DELIMITER")
    #: The dataset a request that names none reads, written ``warehouse$namespace``; unset, such a request is a 404.
    default_dataset: str | None = Field(default=None, alias="MEDIA_DEFAULT_DATASET")

    @model_validator(mode="after")
    def _one_catalog(self) -> Self:
        """Exactly one catalog, refused at load: with none the plane has nowhere to read, and with both it would pick one
        silently."""
        if (self.catalog_uri is None) == (self.catalog_dir is None):
            raise ValueError("set exactly one of MEDIA_CATALOG_URI (the catalog's REST door) and MEDIA_CATALOG_DIR (a local directory catalog)")
        return self

    @field_validator("default_dataset")
    @classmethod
    def _default_dataset_is_warehouse_qualified(cls, value: str | None, info: ValidationInfo) -> str | None:
        """A configured default is refused at load, not at every request, when no table could live in it."""
        if value is not None:
            delimiter = str(info.data.get("catalog_delimiter", CATALOG_DELIMITER))
            _warehouse_qualified(value.split(delimiter), delimiter)
        return value

    def catalog_namespace_id(self, dataset_id: str) -> list[str]:
        """The catalog identifier of a dataset's namespace: the dataset id, split on the delimiter.

        Written as the catalog writes it (``warehouse$namespace``). A name's first segment is its warehouse, and a
        one-segment namespace names a warehouse, which serves no tables, so it needs at least two non-empty segments
        (`docs/rask/COMMIT-EVENTS.md`).

        Raises:
            ValueError: The namespace has fewer than two segments or an empty one.
        """
        namespace = dataset_id.split(self.catalog_delimiter)
        _warehouse_qualified(namespace, self.catalog_delimiter)
        return namespace

    def catalog_table_id(self, dataset_id: str, table: str) -> list[str]:
        """The catalog identifier of a dataset's table: its namespace's segments, then the table, one segment, since the
        delimiter inside it would split the identifier server-side into another namespace.

        Raises:
            ValueError: The namespace is not warehouse-qualified, or the table name is empty or carries the delimiter.
        """
        namespace = self.catalog_namespace_id(dataset_id)
        if not table or self.catalog_delimiter in table:
            raise ValueError(f"catalog table name {table!r} must be one non-empty segment without {self.catalog_delimiter!r}")
        return [*namespace, table]

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
