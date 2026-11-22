"""The dataset descriptor — declared semantic roles + discovered facts, merged.

The *declared* half (LANCE_MEDIA_MERGE §4.2) carries what introspection cannot
know: which fields form row identity, which table/column serve the media
bytes, what to display, how search modes bind to vector columns. It is DATA —
read from a config file in ``config/descriptors/<dataset>.json`` (override,
for datasets we can't rewrite) or from the ``lance_media.descriptor`` schema-
metadata key stamped at table-create time.

:func:`validate_descriptor` enforces the load-bearing invariants against the
live schema at load time (and in tests): identity fields must exist, the media
blob column must really be blob-v2, every vector binding must name an actual
FixedSizeList column with the declared dimension.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from workbench.lancekit.introspect import TableInfo


logger = logging.getLogger(__name__)

DESCRIPTOR_METADATA_KEY = b"lance_media.descriptor"


class Identity(BaseModel):
    """Row identity: the key fields (in order) and the doc-key whitelist pattern."""

    key_fields: list[str]
    doc_key: str = "doc_id"
    doc_key_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"


class DocumentBinding(BaseModel):
    """Where a document's media bytes and thumbnail live, and what kind of media they are.

    ``media_blob``, ``mime``, ``thumbnail`` and ``thumbnail_mime`` each name a COLUMN of ``table``: the mime columns hold
    each row's media type. ``kind`` is the one fact about the media no column states, which element a player hands it
    to; undeclared, a corpus with a time binding is read as video and any other as images.
    """

    table: str
    media_blob: str
    kind: Literal["image", "audio", "video"] | None = None
    mime: str | None = None
    thumbnail: str | None = None
    thumbnail_mime: str | None = None


class TimeBinding(BaseModel):
    start: str
    end: str


class MetadataField(BaseModel):
    field: str
    label: str


class Display(BaseModel):
    title: list[str] = Field(default_factory=list)
    body: str | None = None
    caption: str | None = None
    metadata: list[MetadataField] = Field(default_factory=list)


class VectorBinding(BaseModel):
    """One searchable vector column and how a query reaches its space."""

    table: str
    column: str
    dim: int
    query_encoder: str  # "text" | "image" — which encoder embeds the query
    caption_source: str | None = None  # FTS-able text column in the same space, if any
    #: The model that made the column's vectors, as its writer names it. Vectors of two models are never compared, so
    #: what is measured on them records it.
    model: str | None = None


class ScoreBinding(BaseModel):
    """A column a pipeline wrote a model's confidence in each row to: a number from 0 (it cannot tell) to 1 (it is
    sure), null where it scored none. A project that samples by ``model`` reads its items' scores from it, and asks no
    model for them."""

    table: str
    column: str
    model: str


class EmbeddingBinding(BaseModel):
    """A vector column rows are compared in, and the model that made its vectors, as its writer names it. Unlike a
    search's vector binding it offers no search mode, so it needs no query encoder."""

    table: str
    column: str
    model: str | None = None


class SamplingBinding(BaseModel):
    """What a corpus holds for choosing which rows are labelled next: a score column, and the embeddings rows are told
    apart by. A corpus that declares no embeddings here is compared in its default search's first vector binding."""

    scores: ScoreBinding | None = None
    embeddings: EmbeddingBinding | None = None


class FtsBinding(BaseModel):
    table: str
    column: str
    language: str = "English"


class Search(BaseModel):
    """One SEARCHABLE table, with the bindings that make it searchable.

    Named, and a corpus may declare several, so each searchable table of a dataset can be picked, not only the
    dataset. The shape mirrors `atlas`: a named list, each entry bound to its own table. Display fields, fts and vector
    bindings are all per-TABLE, so they travel with the table rather than sit beside a single privileged one.
    """

    #: Stable key the UI and the `table=` selector use. `default` for a corpus that declares one,
    #: which is what every descriptor on disk says today.
    name: str = "default"
    row_table: str
    fts: FtsBinding | None = None
    vectors: dict[str, VectorBinding] = Field(default_factory=dict)  # mode name → binding
    filterable: list[str] = Field(default_factory=list)
    rerank: bool = False


class AtlasChannel(BaseModel):
    """One categorical colour channel on the atlas: output name ← source column.

    ``column`` names the source column directly; ``broadest_prefix`` instead
    selects the highest-numbered ``<prefix>N`` column present (the broadest
    topic layer, whose index is data-dependent) — so no ``topic_l`` literal
    lives in code. Exactly one of the two is set.
    """

    name: str
    column: str | None = None
    broadest_prefix: str | None = None


class AtlasSpace(BaseModel):
    name: str
    x: str
    y: str
    cluster: str
    source_column: str
    table: str
    channels: list[AtlasChannel] = Field(default_factory=list)


class Declared(BaseModel):
    """The declared half of a descriptor. ``extra`` kept for forward compat."""

    model_config = {"extra": "allow"}

    identity: Identity
    document: DocumentBinding | None = None
    time: TimeBinding | None = None
    display: Display = Display()
    #: Every searchable table this corpus exposes, in declaration order. The FIRST is the default.
    searches: list[Search] = Field(default_factory=list)
    atlas: list[AtlasSpace] = Field(default_factory=list)
    #: What the corpus holds for sampling (a score column, embeddings), if anything.
    sampling: SamplingBinding | None = None

    @property
    def embeddings(self) -> EmbeddingBinding | None:
        """The vectors this corpus's rows are compared in: the ones it declares for sampling, else its default search's
        first vector binding; None for a corpus with neither."""
        if self.sampling is not None and self.sampling.embeddings is not None:
            return self.sampling.embeddings
        first = next(iter(self.search.vectors.values()), None) if self.search else None
        return None if first is None else EmbeddingBinding(table=first.table, column=first.column, model=first.model)

    capabilities: dict[str, str] = Field(default_factory=dict)  # capability → table/column it needs

    @model_validator(mode="before")
    @classmethod
    def _accept_single_search(cls, data: Any) -> Any:
        """`search: {...}` (what every descriptor on disk carries) becomes `searches: [{...}]`.

        A before-validator and not a migration script: descriptors are FILES, written by hand and by
        seeding scripts, and rewriting them all to land one feature would make the feature's blast
        radius the whole corpus estate. `extra="allow"` on this model makes the translation
        load-bearing rather than cosmetic — without popping the key, `search` would survive as an
        untyped extra and quietly shadow nothing while the real list stayed empty.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.get("search")
        if legacy is None:
            return data
        data = dict(data)
        data.pop("search")
        # An explicit `searches` wins: a descriptor carrying both is stating the new shape, and the
        # legacy key is then a leftover rather than an instruction.
        data.setdefault("searches", [legacy])
        return data

    @computed_field
    @property
    def search(self) -> Search | None:
        """The DEFAULT searchable table, or None.

        A property, so the ~15 existing readers of `declared.search` keep working untouched — and a
        COMPUTED field, so the served JSON still carries `search` beside the new `searches`. The
        frontend reads `declared.search` today; dropping it from the wire to add a list would have
        broken every zone in the same commit that added the capability.
        """
        return self.searches[0] if self.searches else None

    def search_named(self, name: str | None) -> Search | None:
        """One declared table by name — `None` selects the default.

        Returns `None` for a name this corpus does not declare, which callers must treat as a 404
        rather than silently falling back to the default: a search that quietly answered from a
        different table than the one asked for would be a wrong answer, not a lenient one.
        """
        if name is None:
            return self.search
        return next((s for s in self.searches if s.name == name), None)


class DatasetDescriptor(BaseModel):
    """Merged view served to clients: dataset id + discovered tables + declared roles.

    ``declared`` is ``None`` for a dataset whose writer stamped no descriptor (or an invalid one, said in ``problem``):
    it is shown from its schema alone, as a plain table, and nothing that needs a declared role is offered on it.
    """

    id: str
    #: The source the dataset is read from (`workbench.sources`), and its kind.
    source: str
    kind: str
    tables: dict[str, TableInfo]
    declared: Declared | None
    #: Why the declared roles could not be used, when a stamp or override exists and is invalid.
    problem: str | None = None
    #: The tables the catalog lists here that could not be opened, each with the reason a person can read.
    unreadable: dict[str, str] = Field(default_factory=dict)
    #: Why the dataset cannot be annotated (its source is not marked annotatable), or ``None`` when it can.
    read_only: str | None = None
    #: A table could not be read right now: the descriptor is not kept, and the next request reads the dataset again.
    transient: bool = Field(default=False, exclude=True)

    def capability_available(self, name: str) -> bool:
        if self.declared is None:
            return False
        target = self.declared.capabilities.get(name)
        if target is None:
            return False
        table, _, column = target.partition(".")
        info = self.tables.get(table)
        if info is None:
            return False
        return column == "" or info.column(column) is not None


def validate_descriptor(declared: Declared, tables: dict[str, TableInfo]) -> list[str]:
    """The P2.2 cross-check: declared roles must exist in the live schema.

    Returns the list of violations (empty = valid). Callers decide whether to
    raise; the loader logs and refuses to serve an invalid descriptor.
    """
    problems: list[str] = []

    # EVERY declared searchable table, not just the default. A corpus can now expose several, and a
    # second one whose row table does not exist would otherwise load fine and 500 the first time
    # anyone selected it.
    seen_names: set[str] = set()
    for search in declared.searches:
        if search.name in seen_names:
            # Names are the `table=` selector's keys, so a duplicate makes the selector ambiguous —
            # and the one it resolves to would depend on declaration order, invisibly.
            problems.append(f"search {search.name!r} is declared twice")
        seen_names.add(search.name)

        info = tables.get(search.row_table)
        if info is None:
            problems.append(f"search {search.name!r}: row_table {search.row_table!r} does not exist")
            continue
        for key in declared.identity.key_fields:
            if info.column(key) is None:
                problems.append(f"identity field {key!r} missing from {search.row_table!r}")

    if declared.document is not None:
        doc_table = tables.get(declared.document.table)
        if doc_table is None:
            problems.append(f"document.table {declared.document.table!r} does not exist")
        else:
            blob_col = doc_table.column(declared.document.media_blob)
            if blob_col is None:
                problems.append(f"document.media_blob {declared.document.media_blob!r} missing")
            elif not blob_col.is_blob:
                problems.append(f"document.media_blob {declared.document.media_blob!r} is not a lance.blob.v2 column")
            # Each is read as a column when a document's media is signed, so one that names none is refused here, when
            # the dataset opens, rather than failing that read.
            for role in ("mime", "thumbnail", "thumbnail_mime"):
                column = getattr(declared.document, role)
                if column is not None and doc_table.column(column) is None:
                    problems.append(f"document.{role} {column!r} is not a column of {declared.document.table!r}")

    for search in declared.searches:
        for mode, binding in search.vectors.items():
            info = tables.get(binding.table)
            column = info.column(binding.column) if info else None
            if column is None:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} missing")
            elif column.vector_dim != binding.dim:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} dim {column.vector_dim} != declared {binding.dim}")
        if search.fts is not None:
            info = tables.get(search.fts.table)
            if info is None or info.column(search.fts.column) is None:
                problems.append(f"fts binding: {search.fts.table}.{search.fts.column} missing")

    sampling = declared.sampling
    for role, binding in (("scores", sampling.scores), ("embeddings", sampling.embeddings)) if sampling else ():
        if binding is None:
            continue
        info = tables.get(binding.table)
        column = info.column(binding.column) if info else None
        if info is None or column is None:
            problems.append(f"sampling.{role}: {binding.table}.{binding.column} missing")
            continue
        if role == "scores" and column.arrow_type not in ("float", "double", "halffloat"):
            problems.append(f"sampling.scores: {binding.table}.{binding.column} is {column.arrow_type}, not a number from 0 to 1")
        if role == "embeddings" and column.vector_dim is None:
            problems.append(f"sampling.embeddings: {binding.table}.{binding.column} is {column.arrow_type}, not a vector column")
        # Rows are found by the corpus's identity, so the table must name a row by it.
        problems.extend(
            f"sampling.{role}: identity field {key!r} missing from {binding.table!r}" for key in declared.identity.key_fields if info.column(key) is None
        )

    for space in declared.atlas:
        info = tables.get(space.table)
        for col in (space.x, space.y, space.cluster):
            if info is None or info.column(col) is None:
                problems.append(f"atlas space {space.name!r}: column {col!r} missing from {space.table!r}")

    return problems
