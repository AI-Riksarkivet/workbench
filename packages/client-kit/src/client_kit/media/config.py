"""The media services' settings: what the viewer, search and the annotator share, on the kit's `Settings`.

Read once by each service's own getter (`viewer.core.config.get_viewer_settings` and its siblings); routers read it off
``state.settings`` (the injected :class:`~client_kit.media.state.AppState`). Only env-varying values live here —
algorithmic constants (RRF k, probe tokens, column-exclude sets) stay as module constants in their feature packages.

Every name is a ``WORKBENCH_*`` one; what only one service reads is that service's own (``VIEWER_*``, ``SEARCH_*``,
``ANNOTATOR_*``, on its own class).
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, PrivateAttr, model_validator

from client_kit.sources import RaskSource, Source, SourceIdentity, Sources, workbench_decides
from service_kit.config import Settings as ServiceSettings
from service_kit.governed.settings import GovernedAuthSettings, LakehouseTokenSettings


class MediaSettings(ServiceSettings):
    """The settings every media service reads: the kit's (`service_kit.config.Settings`: CORS, docs, the body cap, the
    audit trail, telemetry), the sources, and the model servers the viewer reports on and search queries."""

    #: The text-embedding and reranking model servers: search's semantic and reranked queries, and the viewer's health
    #: badge, which reports whether each answers.
    embed_url: str = Field(default="http://127.0.0.1:8001", alias="WORKBENCH_EMBED_URL")
    rerank_url: str = Field(default="http://127.0.0.1:8002", alias="WORKBENCH_RERANK_URL")
    #: Where a dataset's descriptor may be given as a file (`<dataset id>.json`) instead of stamped on its table.
    descriptor_dir: Path = Field(default=Path("config/descriptors"), alias="WORKBENCH_DESCRIPTOR_DIR")
    #: The request-body ceiling for the media apps.
    #:
    #:
    #: These are the apps that accept MULTIPART UPLOADS. A file part is spooled to a SpooledTemporaryFile in FULL
    #: before the handler is entered, so the handler's own `read(cap + 1)` bounds its memory and cannot bound the
    #: landing zone: the cap at the door does.
    #:
    #: 32 MiB, not the kit's 64: it stays comfortably ABOVE `_MAX_UPLOAD_BYTES` so the handler's own 400 — which names
    #: the limit in the caller's terms — is still the message an oversize upload gets, rather than a bare 413 from the
    #: door, and no larger than a voice snippet needs.
    max_body_bytes: int = Field(default=32 * 1024 * 1024, ge=0, alias="WORKBENCH_MAX_BODY_BYTES")

    #: The sources the media plane reads (`client_kit.sources`): the declaration git holds, as the file the deployment
    #: mounts (``WORKBENCH_SOURCES_FILE``) or, for a test or a one-off run, its ``sources`` list inline
    #: (``WORKBENCH_SOURCES``, JSON). Exactly one of the two.
    sources_file: Path | None = Field(default=None, alias="WORKBENCH_SOURCES_FILE")
    sources_inline: list[Source] | None = Field(default=None, alias="WORKBENCH_SOURCES")
    #: The dataset a request that names none reads, written ``<source>~<namespace>``; unset, such a request is a 404.
    default_dataset: str | None = Field(default=None, alias="WORKBENCH_DEFAULT_DATASET")

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


class GovernedMediaSettings(GovernedAuthSettings, LakehouseTokenSettings, MediaSettings):
    """A media service's settings with the person it serves: the viewer's, search's and the annotator's base.

    A source no catalog decides for per person (`client_kit.sources.workbench_decides`) is read only by its declared
    readers, which only the workbench's own OpenFGA can answer; with authorization off every check allows. So a service
    that signs people in and declares such a source refuses to start without that store, rather than serve the source
    to every signed-in person. A ``rask`` directory catalog read as nobody (``identity: none``) is decided by no one at all,
    so with sign-in on it is refused outright. Sign-in off (`WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED`, the local stack)
    serves everyone as `anon` and needs no store.
    """

    @model_validator(mode="after")
    def _every_source_is_decided_for_a_signed_in_person(self) -> Self:
        if not self.oidc_enabled:
            return self
        stand_ins = sorted(source.name for source in self.sources.items if isinstance(source, RaskSource) and source.identity is SourceIdentity.NONE)
        if stand_ins:
            raise ValueError(
                f"the sources {stand_ins} are directory catalogs standing in for a lakehouse, which authenticate nobody and are "
                "decided by nobody: a development stand-in for sign-in off, never served to signed-in people"
            )
        if not self.fga_enabled:
            decided = sorted(source.name for source in self.sources.items if workbench_decides(source))
            if decided:
                raise ValueError(
                    f"the sources {decided} are read only by their declared readers, which the workbench's own OpenFGA decides: "
                    "set WORKBENCH_FGA_ENABLED and its store, or the service would serve them to every signed-in person"
                )
        return self
