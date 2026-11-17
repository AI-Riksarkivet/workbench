"""The sources a deployment reads: each a lance-namespace or a Lance table, declared in git, read through one seam.

A deployment names its sources in one declaration (``WORKBENCH_SOURCES_FILE``, a JSON file the chart renders from its
values and the local stack writes from a file), each by a name and a kind:

- ``rask``: a rask lakehouse. Its catalog is the lakehouse's REST door (``uri``), reached through pylance's
  ``RestNamespace`` with the identity the source names: the person's own token under the deployment's delegation, the
  workbench's own client-credentials token, or none. ``dir`` names a directory catalog laid out as rask lays its catalog
  out (``<warehouse>$<namespace>``), a stand-in for development and the offline suite with nobody to authenticate.
- ``namespace``: any lance-namespace implementation, by the name or class path ``lance_namespace.connect`` takes and its
  properties (`lance_docs/ns_catalog/namespace/index.md`; the directory and REST ones are native).
- ``tables``: a bare Lance table (a path ending ``.lance``) or a directory of them, by URI, read through lance-namespace's
  directory catalog in its V1 shape (`lance_docs/ns_catalog/catalog/dir/index.md` § V1: every ``<name>.lance`` under the
  root is a table of the root namespace) with the manifest off, so a read never writes one.

THE ONE SWITCH. Everything rask-specific belongs to the ``rask`` kind alone: a token addressed to the lakehouse, the
``x-lance-originator`` and ``x-rask-*`` context headers, the warehouse-first id rule, and publish.
Every other source is plain lance-namespace or plain Lance, read with no token through a namespace that can only read
(`client_kit.lancekit.confined`), only tables it lists, only under the locations git declares, and only on the storage
options git declares (:func:`connect_source`).

WHAT IS LABELLED. A lakehouse's datasets are always annotated. Any other source's are only where git marks the source
``annotatable: true`` (:func:`annotatable`): its rows are still only read. The work people do on any source's items is
the annotator's own, in its own database, so labeling needs no lakehouse; only a publish does, and lands in one.

WHO DECIDES A READ. A ``rask`` source read as the person (``identity: person``) is the lakehouse's to decide, on the
person's own token. Every other source, and a ``rask`` source read as the workbench (``identity: service``), has no
catalog that knows the person, so the workbench's own OpenFGA decides: ``reader`` on ``source:<name>``, which the model
writer converges from each source's ``readers`` (organisations and people, :class:`SourceReaders`); a source that names
no reader is read by nobody (:func:`workbench_decides`). ``identity: none`` is a directory stand-in's alone, for
development.

A dataset is a namespace of a source holding tables, at any depth including the root; its id is the source's name, a
``~``, and the namespace's id joined by the source's delimiter (``lake~acme$corpus``, ``archive~``), so two sources never
collide and a link names where its data came from. Every segment of an id a request names is checked before any
catalog sees it (`client_kit.source_names`).

No credential is declared: every property and storage option is held to an allow-list (`client_kit.source_keys`), and a
location carries no user, password, query or fragment. A ``rask`` source's token comes from the person or the
workbench's client, and a store credential from the catalog's vend or the workload's own identity.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self

import lance_namespace
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from client_kit.lancekit.catalog import connect_catalog
from client_kit.lancekit.confined import ConfinedNamespace
from client_kit.source_keys import check_location, check_properties, check_storage
from client_kit.source_names import SOURCE_SEPARATOR, check_segments
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.principal import PrincipalKey
from service_kit.naming import CATALOG_DELIMITER


if TYPE_CHECKING:
    from collections.abc import Callable

    from lance_namespace import LanceNamespace


#: A source's name: a DNS label, so it survives every place an id travels (a URL path segment, a table digest, a log).
SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]

#: An organisation id, as the workbench's organisations are declared (`service_kit.governed.auth.write_model`).
OrgId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class SourceKind(StrEnum):
    RASK = "rask"
    NAMESPACE = "namespace"
    TABLES = "tables"


class SourceIdentity(StrEnum):
    """Who a ``rask`` source is read as."""

    #: The person, on their own token under the deployment's delegation (exchanged, or passed on).
    PERSON = "person"
    #: The workbench's own confidential client: every person reads what the workbench's grants admit.
    SERVICE = "service"
    #: Nobody: a directory catalog standing in for a lakehouse in development, which authenticates no one.
    NONE = "none"


class SourceReaders(BaseModel):
    """Who may read a source the workbench decides for: the members of these organisations, and these people."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    orgs: list[OrgId] = Field(default_factory=list)
    people: list[PrincipalKey] = Field(default_factory=list)


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: SourceName
    #: What a person reading the dataset list sees beside the name.
    title: str = ""
    #: Who may read it, where the workbench decides (:func:`workbench_decides`); none named, nobody reads it.
    readers: SourceReaders = Field(default_factory=SourceReaders)


class RaskSource(_Source):
    """A rask lakehouse: its catalog's REST door, or a directory catalog standing in for it."""

    kind: Literal[SourceKind.RASK] = SourceKind.RASK
    uri: str | None = None
    dir: Path | None = None
    delimiter: str = Field(default=CATALOG_DELIMITER, min_length=1)
    identity: SourceIdentity = SourceIdentity.PERSON

    @model_validator(mode="after")
    def _one_catalog(self) -> Self:
        if (self.uri is None) == (self.dir is None):
            raise ValueError(f"rask source {self.name!r} names exactly one of `uri` (the lakehouse catalog's REST door) and `dir` (a directory catalog)")
        if self.dir is not None and self.identity is not SourceIdentity.NONE:
            raise ValueError(f"rask source {self.name!r} is a directory catalog, which authenticates nobody: its identity is `none`")
        if self.uri is not None:
            check_location(self.uri)
            if self.identity is SourceIdentity.NONE:
                raise ValueError(f"rask source {self.name!r} is a lakehouse's REST door, which is read as the person or the workbench, never as nobody")
        if self.identity is SourceIdentity.PERSON and (self.readers.orgs or self.readers.people):
            raise ValueError(f"rask source {self.name!r} is read as the person, so the lakehouse decides who reads it: it names no `readers`")
        return self


class _LabelledOnlyIfMarked(_Source):
    #: Whether its datasets may be sent to a project and labelled (:func:`annotatable`): off unless git marks it.
    annotatable: bool = False


class NamespaceSource(_LabelledOnlyIfMarked):
    """A lance-namespace implementation the allow-list knows (`client_kit.source_keys`), by name, and its properties."""

    kind: Literal[SourceKind.NAMESPACE] = SourceKind.NAMESPACE
    impl: str = Field(min_length=1)
    properties: dict[str, str] = Field(default_factory=dict)
    #: Object-store options its tables are read on, in place of any the catalog answers.
    storage: dict[str, str] = Field(default_factory=dict)
    #: Where its tables may lie: a DescribeTable naming a location under none of these is refused. A directory catalog's
    #: is its root; every other implementation names its own.
    locations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _allowed(self) -> Self:
        check_properties(self.impl, self.properties)
        check_storage(self.storage)
        for location in self.locations:
            check_location(location)
        if self.impl == "dir" and "root" not in self.properties:
            raise ValueError(f"source {self.name!r} is a directory catalog and names no `root`")
        if self.impl != "dir" and not self.locations:
            raise ValueError(f"source {self.name!r} names no `locations`: the store prefixes its tables may be read under")
        return self

    @property
    def roots(self) -> list[str]:
        return [self.properties["root"]] if self.impl == "dir" and not self.locations else list(self.locations)

    @property
    def delimiter(self) -> str:
        """The delimiter the implementation joins an id with: its ``delimiter`` property, else the spec's default
        (`lance_docs/ns_catalog/spec.yaml`: "When not specified, the `$` delimiter must be used")."""
        return self.properties.get("delimiter") or CATALOG_DELIMITER


class TablesSource(_LabelledOnlyIfMarked):
    """A bare Lance table (a URI ending ``.lance``) or a directory of them, read through the directory catalog's V1 shape."""

    kind: Literal[SourceKind.TABLES] = SourceKind.TABLES
    uri: str = Field(min_length=1)
    #: Object-store options (`lance_docs/lance/guide/object_store.md`), each passed as the directory catalog's
    #: ``storage.<name>`` property: a region, an endpoint, never a key.
    storage: dict[str, str] = Field(default_factory=dict)

    @field_validator("storage")
    @classmethod
    def _allowed_storage(cls, value: dict[str, str]) -> dict[str, str]:
        return check_storage(value)

    @field_validator("uri")
    @classmethod
    def _plain_uri(cls, value: str) -> str:
        return check_location(value)

    @property
    def delimiter(self) -> str:
        return CATALOG_DELIMITER

    @property
    def root(self) -> str:
        """The directory the catalog reads: the URI itself, or a single table's parent."""
        return self.uri.rstrip("/").rsplit("/", 1)[0] if self.single is not None else self.uri

    @property
    def single(self) -> str | None:
        """The one table a ``.lance`` URI names (its name without the suffix), or ``None`` for a directory."""
        last = self.uri.rstrip("/").rsplit("/", 1)[-1]
        return last.removesuffix(".lance") if last.endswith(".lance") and len(last) > len(".lance") else None


type Source = Annotated[RaskSource | NamespaceSource | TablesSource, Field(discriminator="kind")]


class Located(BaseModel):
    """A qualified name resolved: its source and the id within it (a namespace's, or a table's)."""

    model_config = ConfigDict(frozen=True)

    source: RaskSource | NamespaceSource | TablesSource
    identifier: list[str]


class Sources(BaseModel):
    """Every source one process reads, by name; at most one lakehouse."""

    model_config = ConfigDict(frozen=True)

    items: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        names = [source.name for source in self.items]
        if len(set(names)) != len(names):
            raise ValueError(f"source names must be distinct: {names}")
        if sum(isinstance(source, RaskSource) for source in self.items) > 1:
            # One lakehouse is where a publish lands, so a second would leave it undecided.
            raise ValueError("at most one source is a rask lakehouse")
        return self

    @classmethod
    def read(cls, path: Path) -> Sources:
        """The declaration at ``path``: ``{"sources": [...]}``. A local path a source names relative (a lakehouse's ``dir``, a
        directory catalog's ``root``, a ``tables`` source's ``uri``) is read from the declaration's own directory, so one
        file declares a set of sources laid out beside it."""
        base = path.parent
        declared = cls(items=json.loads(path.read_text())["sources"])
        return cls(items=[_beside(source, base) for source in declared.items])

    @property
    def lakehouse(self) -> RaskSource | None:
        """The rask source, which alone holds the annotation tables and is published to."""
        return next((source for source in self.items if isinstance(source, RaskSource)), None)

    def named(self, name: str) -> RaskSource | NamespaceSource | TablesSource:
        """Raises:
        ValueError: No source has that name."""
        found = next((source for source in self.items if source.name == name), None)
        if found is None:
            raise ValueError(f"no source is named {name!r}")
        return found

    def locate(self, qualified: str) -> Located:
        """``<source>~<id>`` as its source and the id within it, split on the source's delimiter; the empty id is the root.

        Raises:
            ValueError: The name carries no source, names no declared one, or holds an empty segment.
        """
        name, separator, rest = qualified.partition(SOURCE_SEPARATOR)
        if not separator:
            raise ValueError(f"{qualified!r} names no source: write it <source>{SOURCE_SEPARATOR}<id>")
        source = self.named(name)
        identifier = check_segments(rest.split(source.delimiter) if rest else [], source.delimiter)
        return Located(source=source, identifier=identifier)

    def dataset(self, qualified: str) -> Located:
        """A dataset's id resolved; a lakehouse dataset is warehouse-qualified, since a one-segment name in rask is a
        warehouse, which serves no tables (`docs/rask/COMMIT-EVENTS.md`).

        Raises:
            ValueError: As :meth:`locate`, or a lakehouse id with fewer than two segments.
        """
        located = self.locate(qualified)
        if isinstance(located.source, RaskSource) and len(located.identifier) < 2:
            delimiter = located.source.delimiter
            raise ValueError(f"lakehouse dataset {qualified!r} is not warehouse-qualified: write it {located.source.name}:<warehouse>{delimiter}<namespace>")
        return located

    def table(self, qualified_dataset: str, table: str) -> Located:
        """One table of a dataset.

        Raises:
            ValueError: As :meth:`dataset`, or the table name is empty or carries the source's delimiter, which would split
                it into another namespace server-side.
        """
        located = self.dataset(qualified_dataset)
        check_segments([table], located.source.delimiter)
        return Located(source=located.source, identifier=[*located.identifier, table])

    def table_named(self, qualified: str) -> Located:
        """A table by its qualified name (``<source>~<namespace>$<table>``): a lakehouse table is three or more segments,
        and a source naming one ``.lance`` table holds that table alone.

        Raises:
            ValueError: As :meth:`locate`, or the name cannot be a table of its source.
        """
        located = self.locate(qualified)
        source, identifier = located.source, located.identifier
        if not identifier or (isinstance(source, RaskSource) and len(identifier) < 3):
            raise ValueError(f"{qualified!r} is not a table of source {source.name!r}")
        if isinstance(source, TablesSource) and source.single is not None and identifier != [source.single]:
            raise ValueError(f"source {source.name!r} holds the one table {source.single!r}")
        return located


def _local(location: str) -> bool:
    return "://" not in location and not Path(location).is_absolute()


def _beside(source: RaskSource | NamespaceSource | TablesSource, base: Path) -> RaskSource | NamespaceSource | TablesSource:
    if isinstance(source, RaskSource) and source.dir is not None and not source.dir.is_absolute():
        return source.model_copy(update={"dir": base / source.dir})
    if isinstance(source, NamespaceSource) and source.impl == "dir" and _local(root := source.properties.get("root", "/")):
        return source.model_copy(update={"properties": {**source.properties, "root": str(base / root)}})
    if isinstance(source, TablesSource) and _local(source.uri):
        return source.model_copy(update={"uri": str(base / source.uri)})
    return source


def qualify(source: RaskSource | NamespaceSource | TablesSource, identifier: list[str]) -> str:
    """The qualified name of ``identifier`` in ``source``."""
    return f"{source.name}{SOURCE_SEPARATOR}{source.delimiter.join(identifier)}"


def workbench_decides(source: RaskSource | NamespaceSource | TablesSource) -> bool:
    """Whether the workbench's own OpenFGA decides who reads ``source`` (``reader`` on ``source:<name>``): every source no
    catalog decides for per person, a lakehouse read as the workbench among them."""
    if isinstance(source, RaskSource):
        return source.identity is SourceIdentity.SERVICE
    return True


def annotatable(source: RaskSource | NamespaceSource | TablesSource) -> str | None:
    """Why a dataset of ``source`` cannot be annotated, or ``None`` when it can: a lakehouse's always can, and any other
    source's only when git marks it ``annotatable``."""
    if isinstance(source, RaskSource) or source.annotatable:
        return None
    return f"source {source.name!r} is not marked annotatable"


def connect_source(
    source: RaskSource | NamespaceSource | TablesSource,
    *,
    person: Callable[[], str] | None,
    service: Callable[[], str] | None,
    headers: dict[str, str] | None = None,
) -> LanceNamespace:
    """The namespace client ``source`` is read through, presenting the token its identity names.

    ``person`` and ``service`` answer the person's and the workbench's own token for the lakehouse; only a ``rask`` source
    over REST ever presents one, and only the one its identity names. ``headers`` (``x-lance-originator``, the run rule's)
    are the lakehouse's context headers, refused for any other source.

    Every other source is reached through :class:`~client_kit.lancekit.confined.ConfinedNamespace`: read-only, only the
    tables it lists, under the locations it is declared to read, on the storage options it is declared with.

    Raises:
        TypeError: ``headers`` were given for a source other than a lakehouse.
        ServiceUnavailableError: A lakehouse read as the workbench, and the process names no client to read it as.
    """
    if isinstance(source, RaskSource):
        if source.dir is not None:
            return lance_namespace.connect("dir", {"root": str(source.dir)})
        if source.uri is None:
            raise TypeError("a rask source's validator admits none without a catalog")
        if source.identity is SourceIdentity.SERVICE and service is None:
            raise ServiceUnavailableError(f"source {source.name!r} is read as the workbench, and this service names no confidential client")
        token = service if source.identity is SourceIdentity.SERVICE else person
        return connect_catalog(source.uri, token=token, delimiter=source.delimiter, headers=headers)
    if headers:
        raise TypeError(f"source {source.name!r} is not a lakehouse: it takes no lakehouse context header")
    storage = {f"storage.{k}": v for k, v in source.storage.items()}
    if isinstance(source, NamespaceSource):
        inner = lance_namespace.connect(source.impl, dict(source.properties) | (storage if source.impl == "dir" else {}))
        return ConfinedNamespace(inner, name=source.name, roots=source.roots, storage=source.storage, only=None, delimiter=source.delimiter)
    inner = lance_namespace.connect("dir", {"root": source.root, "manifest_enabled": "false", "dir_listing_enabled": "true"} | storage)
    return ConfinedNamespace(inner, name=source.name, roots=[source.root], storage=source.storage, only=source.single, delimiter=source.delimiter)


__all__ = [
    "SOURCE_SEPARATOR",
    "Located",
    "NamespaceSource",
    "RaskSource",
    "Source",
    "SourceIdentity",
    "SourceKind",
    "SourceReaders",
    "Sources",
    "TablesSource",
    "annotatable",
    "connect_source",
    "qualify",
    "workbench_decides",
]
