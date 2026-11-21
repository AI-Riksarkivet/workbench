"""The Atlas, Graph and Tree of a table nobody stamped, drawn from the columns a person picked.

A table with no workbench descriptor still answers three questions once a person names its columns: where each row sits
on a map (two numeric columns, or one vector column projected to two dimensions), how its rows link (an edge table's
from and to, or a self-referencing id and points-to), and how they nest (an id and a parent, or one path). The person
picks; the viewer reads, and nothing is written to the source and no index is built.

Every read here is bounded and says what it cut:

- **Rows.** A table larger than the read's cap is read as an evenly spaced sample: the row at every ``total / cap``-th
  position, through Lance's random read (``LanceDataset.take``, `lance_docs/lance/guide/read_and_write.md` § Random
  read), so the same version always gives the same sample. A table within the cap is read whole.
- **Bytes.** Every text cell is cut in the read's own projection (``left``, as the plain rows read it): a key at
  :data:`KEY_CHARS`, a path at :data:`PATH_CHARS`, a label at :data:`LABEL_CHARS`; a vector column is read only for the
  sampled rows, and the sample shrinks so the vectors and their copies stay within :data:`VECTOR_BYTES`.
- **Time.** A read stops between chunks once :data:`DEADLINE_SECONDS` have passed and says so; work on what was read
  stops at the same deadline, and a build nobody waits for any more stops at its next check (:class:`Budget`).
- **Shape.** The graph keeps the heaviest edges and the best-connected nodes, the tree the largest branches to a depth
  of :data:`TREE_DEPTH`, and each reports what it left out.
- **Numbers.** A non-finite weight, size or position is left out and counted; every JSON answer is strict
  (:func:`strict_json`).

A node is named by a typed key (:func:`_keys`: ``i:7`` for an integer column's 7, ``s:7`` for a text column's "7"), and
the rows a node stands for are decided by the same code that counted them: :func:`graph_members` and
:func:`tree_members` repeat the derivation's own read and keep the rows its definition puts under the node, so the count
beside a node and the rows listed for it are the same rows.

Column names come from the request: each is looked up in the table's schema (:func:`picked`) before anything is read,
and a read names columns only through the one quoting the plain rows use (`plain_rows.quote_column`). The 2-D projection
of a vector column is a principal component analysis: numpy's LAPACK ``eigh`` on the sample's covariance, deterministic.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from enum import StrEnum
from typing import Any, Final

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from pydantic import BaseModel, Field

from viewer.services.plain_rows import MAX_OFFSET, MAX_PAGE, ROW_ID, ColumnKind, ColumnView, RowsPage, quote_column
from workbench.arrow_ipc import encode_arrow_stream
from workbench.exceptions import ServiceUnavailableError, ValidationError


#: How long a derived read may take, reading and working on what it read.
DEADLINE_SECONDS: Final = 15.0
#: How many rows one take reads at most, between which the deadline is checked; the first take reads
#: :data:`FIRST_CHUNK_ROWS`, and each later one as many rows as :data:`CHUNK_BYTES` holds at the widths seen so far.
CHUNK_ROWS: Final = 100_000
FIRST_CHUNK_ROWS: Final = 2_000
CHUNK_BYTES: Final = 16 * 1024 * 1024
#: The most bytes of values one derived read holds (Arrow's own measure of what it took); the read stops there, says so,
#: and works on what it took. With ``VIEWER_DERIVED_BUILDS`` reads at once, this is what bounds a build's memory.
READ_BYTES: Final = 96 * 1024 * 1024
#: How many rows a loop over rows handles between two checks of the deadline.
CHECK_EVERY: Final = 50_000
#: The most points an atlas draws; a larger table is sampled.
ATLAS_POINTS: Final = 20_000
#: The most bytes a vector sample may take: the column as read, its float64 copy and the centred copy.
VECTOR_BYTES: Final = 64 * 1024 * 1024
#: The widest vector projected: the covariance is ``dim * dim``.
VECTOR_MAX_DIM: Final = 2048
#: How much of a label travels with a point or a node.
LABEL_CHARS: Final = 120
#: How much of an id or a reference a read takes; a longer one is cut there, and counted.
KEY_CHARS: Final = 256
#: How much of a path a read takes, and of each of its levels; a longer one is cut there, and counted.
PATH_CHARS: Final = 2048
SEGMENT_CHARS: Final = 120
#: The most categories a colour keeps; the rest are one "other".
MAX_CATEGORIES: Final = 48
#: A numeric colour column with at most this many distinct values is coloured as categories.
CATEGORY_LIKE: Final = 24
#: How many rows an edge graph and a path tree read at most: their rows are grouped by Arrow.
STRUCTURE_ROWS: Final = 1_000_000
#: How many rows a self-referencing graph and a parent tree read at most: each of their rows is a node walked in Python
#: (measured on a 3,000,000-row table: 400,000 rows a second and about 550 MB per million).
NODE_ROWS: Final = 400_000
#: The most edges and nodes a graph answers.
GRAPH_EDGES: Final = 3_000
GRAPH_NODES: Final = 1_500
#: The most nodes a tree answers, the most children one node shows, and the deepest it goes.
TREE_NODES: Final = 2_000
TREE_CHILDREN: Final = 40
TREE_DEPTH: Final = 16
#: How many rows a table's profile, and a set-up's check, is read from.
PROFILE_ROWS: Final = 2_000
#: How many distinct values a profile counts before it says "at least".
PROFILE_DISTINCT: Final = 1_000
#: How many key columns a profile compares with each other for references (the comparison is pairwise).
PROFILE_KEY_COLUMNS: Final = 16
#: The largest answer a derived read returns; the bounds above keep every answer well under it.
ANSWER_BYTES: Final = 16 * 1024 * 1024
#: The separators a path column is recognised by, in the order a tie is broken.
SEPARATORS: Final = ("/", " > ", ">", "\\", ".", ":", "|")

_NUMERIC: Final = frozenset({ColumnKind.INTEGER, ColumnKind.FLOAT, ColumnKind.DECIMAL})
_KEYS: Final = frozenset({ColumnKind.TEXT, ColumnKind.INTEGER})
_LABELS: Final = frozenset(
    {ColumnKind.TEXT, ColumnKind.INTEGER, ColumnKind.FLOAT, ColumnKind.DECIMAL, ColumnKind.BOOLEAN, ColumnKind.DATE, ColumnKind.TIMESTAMP}
)
_GROUPS: Final = frozenset({ColumnKind.TEXT, ColumnKind.INTEGER, ColumnKind.BOOLEAN})
_COLOURS: Final = frozenset({ColumnKind.TEXT, ColumnKind.BOOLEAN, *_NUMERIC})
#: The kinds a profile reads values of: what a set-up's suggestions weigh.
_PROFILED: Final = frozenset({ColumnKind.TEXT, ColumnKind.BOOLEAN, *_NUMERIC})


class Abandoned(Exception):
    """Nobody waits for this build any more: it stops, and nothing is kept."""


#: The share of the deadline a read may take; the rest is for the work on what it read.
READ_SHARE: Final = 2 / 3


class Budget:
    """A derived read's deadline, the part of it the read may take, and whether anyone still waits for it."""

    def __init__(self, deadline: float, cancelled: threading.Event | None = None, *, read_until: float | None = None) -> None:
        self.deadline = deadline
        self.read_until = deadline if read_until is None else read_until
        self.cancelled = cancelled or threading.Event()

    def expired(self) -> bool:
        return time.monotonic() > self.deadline

    def reading_expired(self) -> bool:
        return time.monotonic() > self.read_until

    def check(self) -> None:
        """Stop here when nobody waits, or the deadline passed while working on what was read.

        Raises:
            Abandoned: Every caller went away.
            ServiceUnavailableError: The work took longer than :data:`DEADLINE_SECONDS`.
        """
        if self.cancelled.is_set():
            raise Abandoned
        if self.expired():
            raise ServiceUnavailableError(f"this view took longer than {DEADLINE_SECONDS:g} s to draw; pick columns over fewer rows")


def budget(cancelled: threading.Event | None = None) -> Budget:
    """A derived read's budget from now: :data:`DEADLINE_SECONDS`, the read stopping at :data:`READ_SHARE` of it."""
    now = time.monotonic()
    return Budget(now + DEADLINE_SECONDS, cancelled, read_until=now + DEADLINE_SECONDS * READ_SHARE)


class ReadExtent(BaseModel):
    """How much of the table a derived read saw."""

    #: The table's rows at the version read.
    total: int
    #: The rows the read took.
    read: int
    #: The rows were an evenly spaced sample, not the whole table.
    sampled: bool
    #: Why the read stopped before its sample was complete (the deadline), or ``None``.
    cut: str | None = None


def picked(columns: list[ColumnView], name: str, kinds: frozenset[ColumnKind], role: str) -> ColumnView:
    """The column ``name`` names, checked against the table's schema and the kinds ``role`` reads.

    Raises:
        ValidationError: The table has no such column, or it holds values ``role`` cannot read.
    """
    found = next((c for c in columns if c.name == name), None)
    if found is None:
        raise ValidationError(f"the table has no column {name!r} for the {role}")
    if found.kind not in kinds:
        wanted = ", ".join(sorted(k.value for k in kinds))
        raise ValidationError(f"column {name!r} holds {found.kind} values; the {role} reads {wanted}")
    return found


def _apart(**roles: ColumnView | None) -> None:
    """Refuse one column picked for two roles that must differ (x and y, from and to, id and parent).

    Raises:
        ValidationError: Two of ``roles`` name the same column.
    """
    named = [(role, column.name) for role, column in roles.items() if column is not None]
    for i, (role, name) in enumerate(named):
        for other, again in named[i + 1 :]:
            if name == again:
                raise ValidationError(f"the {role} and the {other} must be different columns; both name {name!r}")


def _read_as(column: ColumnView, chars: int | None = None) -> str:
    """What a read takes of ``column``: a text cell cut to one more than ``chars`` (so a cut cell says so), anything
    else whole; the name quoted by the plain rows' one quoting."""
    quoted = quote_column(column.name)
    return f"left({quoted}, {chars + 1})" if chars is not None and column.kind is ColumnKind.TEXT else quoted


def _take(ds: lance.LanceDataset, positions: list[int], projection: dict[str, str]) -> pa.Table:
    """One chunk of a spread read: the rows at ``positions``, through ``projection``."""
    return ds.take(positions, columns=projection)


def read_spread(ds: lance.LanceDataset, projection: dict[str, str], cap: int, *, within: Budget) -> tuple[pa.Table, ReadExtent]:
    """``projection`` and the row id of at most ``cap`` rows of ``ds``: all of them when the table holds no more, else the
    rows at evenly spaced positions, read a chunk at a time while ``within`` allows.

    Raises:
        Abandoned: Nobody waits for the read any more.
        ServiceUnavailableError: The deadline passed before a single row was read.
    """
    total = ds.count_rows()
    wanted = {**projection, ROW_ID: ROW_ID}
    positions = np.arange(total, dtype=np.int64) if total <= cap else (np.arange(cap, dtype=np.int64) * total) // cap
    parts: list[pa.Table] = []
    cut: str | None = None
    start, held, step = 0, 0, min(FIRST_CHUNK_ROWS, CHUNK_ROWS)
    while start < len(positions):
        if within.cancelled.is_set():
            raise Abandoned
        if within.reading_expired():
            if not parts:
                raise ServiceUnavailableError(f"this table did not answer within {DEADLINE_SECONDS * READ_SHARE:g} s; try again")
            cut = f"the read stopped after {DEADLINE_SECONDS * READ_SHARE:g} s"
            break
        if held >= READ_BYTES:
            cut = f"the read stopped at {READ_BYTES // (1024 * 1024)} MB of values"
            break
        part = _take(ds, positions[start : start + step].tolist(), wanted)
        parts.append(part)
        start += step
        held += part.nbytes
        width = max(1, held // max(1, sum(p.num_rows for p in parts)))
        step = max(1, min(CHUNK_ROWS, CHUNK_BYTES // width, (READ_BYTES - held) // width + 1))
    table = _plain_strings(pa.concat_tables(parts) if parts else _take(ds, [], wanted))
    return table, ReadExtent(total=total, read=table.num_rows, sampled=total > len(positions) or cut is not None, cut=cut)


def _plain_strings(table: pa.Table) -> pa.Table:
    """``table`` with each text column as plain ``string``: a cut cell (``left``) reads as ``string_view``, which some
    Arrow kernels (``take``, ``group_by``) do not take."""
    fields = [pa.field(f.name, pa.string()) if pa.types.is_string_view(f.type) else f for f in table.schema]
    return table.cast(pa.schema(fields)) if fields != list(table.schema) else table


def _call(name: str, *args: object, options: pc.FunctionOptions | None = None) -> Any:
    """Arrow compute kernel ``name`` on ``args``: the kernels pyarrow generates at import have no static names, so
    they are called by name."""
    return pc.call_function(name, list(args), options)


def _count(mask: object) -> int:
    """How many values of a boolean array are true."""
    return int(_call("sum", mask).as_py() or 0)


def _column(table: pa.Table, name: str) -> pa.Array:
    return table.column(name).combine_chunks()


def _text(column: pa.Array, chars: int = LABEL_CHARS) -> pa.Array:
    """``column`` as text, each value cut to ``chars``."""
    return _call("utf8_slice_codeunits", column.cast(pa.string()), options=pc.SliceOptions(0, chars))


def _keys(column: pa.Array, kind: ColumnKind) -> tuple[pa.Array, int]:
    """A node's typed key for each value of an id or reference column (``i:7`` for an integer, ``s:7`` for text, so
    the two never meet), and how many text values were cut at :data:`KEY_CHARS`."""
    text = column.cast(pa.string())
    clipped = 0
    if kind is ColumnKind.TEXT:
        clipped = _count(_call("greater", _call("utf8_length", text), pa.scalar(KEY_CHARS)))
        text = _text(text, KEY_CHARS)
    prefix = "i:" if kind is ColumnKind.INTEGER else "s:"
    return _call("binary_join_element_wise", pa.scalar(prefix), text, pa.scalar("")), clipped


def _shown(key: str) -> str:
    """A typed key as a person reads it."""
    return key[2:]


def _finite(values: pa.Array) -> pa.Array:
    """Whether each value is a finite number (a null is not)."""
    return _call("coalesce", _call("is_finite", values.cast(pa.float64())), pa.scalar(value=False))


def strict_json(model: BaseModel, *, exclude_none: bool = False) -> bytes:
    """``model`` as JSON a browser's parser reads: a non-finite number anywhere is a defect here, never ``NaN``.

    Raises:
        ValidationError: The answer is larger than :data:`ANSWER_BYTES`.
    """
    body = json.dumps(model.model_dump(mode="json", exclude_none=exclude_none), allow_nan=False, ensure_ascii=False).encode()
    return bounded(body)


def tree_json(tree: DerivedTree) -> bytes:
    """A tree as strict JSON: a node's absent ``id``, ``value`` or ``children`` left out, the diagnostics whole."""
    body = tree.model_dump(mode="json", exclude={"hierarchy"})
    body["hierarchy"] = tree.hierarchy.model_dump(mode="json", exclude_none=True)
    return bounded(json.dumps(body, allow_nan=False, ensure_ascii=False).encode())


def bounded(body: bytes) -> bytes:
    """``body``, or a refusal when it is larger than any answer here should be.

    Raises:
        ValidationError: ``body`` is larger than :data:`ANSWER_BYTES`.
    """
    if len(body) > ANSWER_BYTES:
        raise ValidationError(f"this view's answer would be larger than {ANSWER_BYTES // (1024 * 1024)} MB; pick columns with shorter values")
    return body


class Picks(BaseModel):
    """What every set-up's read names besides its columns: the table version it reads (unset, the latest)."""

    version: int | None = Field(default=None, ge=1)

    def cache_key(self) -> str:
        """The columns picked, as the answer is kept by: the version is the cache key's own part."""
        return self.model_dump_json(exclude_none=True, exclude={"version"})


# ── Atlas ────────────────────────────────────────────────────────────────────────────────────────────────────────────


class AtlasSpec(Picks):
    """What a person picked for an atlas: two numeric columns, or one vector column; a colour and a label."""

    x: str | None = Field(default=None, min_length=1, max_length=256)
    y: str | None = Field(default=None, min_length=1, max_length=256)
    vector: str | None = Field(default=None, min_length=1, max_length=256)
    color: str | None = Field(default=None, min_length=1, max_length=256)
    label: str | None = Field(default=None, min_length=1, max_length=256)


class ProjectionMethod(StrEnum):
    COLUMNS = "columns"
    PCA = "pca"


def _unit(values: np.ndarray) -> np.ndarray:
    """``values`` moved and scaled into ``[-1, 1]``, so a column of any magnitude survives float16 on the wire. Each end
    is halved before they are combined, so no sum or difference of two finite numbers overflows."""
    lo, hi = float(values.min()), float(values.max())
    middle = lo / 2 + hi / 2
    half = hi / 2 - lo / 2
    return (values - middle) / (half or 1.0)


def pca_2d(vectors: np.ndarray) -> tuple[np.ndarray, float]:
    """The first two principal components of ``vectors`` (one row per point) and the share of the variance they keep.

    The covariance's eigenvectors from numpy's ``eigh``; each component's sign is fixed so its largest loading is
    positive, which makes the map the same on every read of the same rows.
    """
    centred = vectors - vectors.mean(axis=0)
    eigenvalues, eigenvectors = np.linalg.eigh(centred.T @ centred)
    top = eigenvectors[:, ::-1][:, :2]
    if top.shape[1] < 2:
        top = np.pad(top, ((0, 0), (0, 2 - top.shape[1])))
    signs = np.sign(top[np.abs(top).argmax(axis=0), np.arange(2)])
    top = top * np.where(signs == 0, 1, signs)
    variance = float(eigenvalues.sum())
    kept = float(eigenvalues[::-1][:2].sum()) / variance if variance > 0 else 0.0
    return centred @ top, kept


def vector_cap(column: ColumnView, arrow_type: pa.DataType) -> int:
    """How many rows of a vector column an atlas reads: :data:`ATLAS_POINTS`, fewer where the vectors as read, their
    float64 copy and the centred copy would pass :data:`VECTOR_BYTES`."""
    item = arrow_type.value_type.bit_width // 8 if pa.types.is_fixed_size_list(arrow_type) else 8
    per_row = (column.dim or 1) * (item + 16)
    return max(1, min(ATLAS_POINTS, VECTOR_BYTES // per_row))


def _colour(column: pa.Array, kind: ColumnKind) -> tuple[pa.Array, str]:
    """A colour channel: categories as a dictionary (the :data:`MAX_CATEGORIES` most frequent, the rest "other"), or a
    number per point, a missing one NaN (drawn as no value)."""
    if kind in _NUMERIC and len(column.drop_null().unique()) > CATEGORY_LIKE:
        numbers = column.cast(pa.float64())
        return _call("if_else", _finite(numbers), numbers, pa.scalar(math.nan)).cast(pa.float32()), "number"
    text = _text(column)
    counts = text.drop_null().value_counts().to_pylist()
    counts.sort(key=lambda c: (-c["counts"], c["values"]))
    kept = {c["values"] for c in counts[:MAX_CATEGORIES]}
    if len(counts) > MAX_CATEGORIES:
        common = _call("or_kleene", _call("is_in", text, options=pc.SetLookupOptions(value_set=pa.array(sorted(kept)))), text.is_null())
        text = _call("if_else", common, text, pa.scalar("other"))
    return _dictionary(text), "category"


def _dictionary(values: pa.Array) -> pa.Array:
    return pc.fill_null(values.cast(pa.string()), "").dictionary_encode().cast(pa.dictionary(pa.int32(), pa.string()))


def build_atlas(ds: lance.LanceDataset, columns: list[ColumnView], spec: AtlasSpec, *, within: Budget) -> bytes:
    """The points of ``spec`` as one Arrow IPC stream: float16 ``x`` and ``y`` scaled into ``[-1, 1]``, each point's
    ``rowid``, and a ``color`` channel and a ``label`` where picked. The schema's metadata says how much was read
    (``total``, ``read``, ``count``, ``sampled``, ``cut``), how many rows had no finite position (``dropped``), how the
    positions were made (``method``, and for a projection the ``variance`` it keeps) and how the colour reads.

    Raises:
        ValidationError: The pick names no position, both kinds of position, one column for both x and y, or a column
            the table lacks or cannot read for its role.
    """
    by_columns = spec.x is not None or spec.y is not None
    if (spec.vector is not None) == by_columns or (by_columns and (spec.x is None or spec.y is None)):
        raise ValidationError("an atlas is placed by two numeric columns (x and y) or by one vector column, not both")
    projection: dict[str, str] = {}
    cap = ATLAS_POINTS
    if spec.vector is not None:
        vector = picked(columns, spec.vector, frozenset({ColumnKind.VECTOR}), "position")
        dim = vector.dim or 0
        if dim < 2 or dim > VECTOR_MAX_DIM:
            raise ValidationError(f"column {vector.name!r} holds {dim}-value vectors; a projection reads 2 to {VECTOR_MAX_DIM}")
        cap = vector_cap(vector, ds.schema.field(vector.name).type)
        projection["v"] = _read_as(vector)
    else:
        x = picked(columns, spec.x or "", _NUMERIC, "x position")
        y = picked(columns, spec.y or "", _NUMERIC, "y position")
        _apart(x=x, y=y)
        projection["x"], projection["y"] = _read_as(x), _read_as(y)
    colour = picked(columns, spec.color, _COLOURS, "colour") if spec.color else None
    label = picked(columns, spec.label, _LABELS, "label") if spec.label else None
    if colour is not None:
        projection["c"] = _read_as(colour, LABEL_CHARS)
    if label is not None:
        projection["l"] = _read_as(label, LABEL_CHARS)
    table, extent = read_spread(ds, projection, cap, within=within)
    within.check()

    if spec.vector is not None:
        cells = _column(table, "v")
        dim = cells.type.list_size
        matrix = np.asarray(cells.flatten().to_numpy(zero_copy_only=False), dtype=np.float64).reshape(len(cells), dim)
        keep = np.asarray(cells.is_valid().to_numpy(zero_copy_only=False), dtype=bool) & np.isfinite(matrix).all(axis=1)
        table, matrix = table.filter(pa.array(keep)), matrix[keep]
        xy, variance = pca_2d(matrix) if len(matrix) else (np.zeros((0, 2)), 0.0)
        xs, ys, method = xy[:, 0], xy[:, 1], ProjectionMethod.PCA
    else:
        keep_mask = _call("and", _finite(_column(table, "x")), _finite(_column(table, "y")))
        table = table.filter(keep_mask)
        xs = _column(table, "x").cast(pa.float64()).to_numpy()
        ys = _column(table, "y").cast(pa.float64()).to_numpy()
        variance, method = math.nan, ProjectionMethod.COLUMNS
    within.check()

    if len(xs):
        xs, ys = _unit(np.asarray(xs)), _unit(np.asarray(ys))
    arrays: list[pa.Array] = [
        pa.array(np.asarray(xs, dtype=np.float16), pa.float16()),
        pa.array(np.asarray(ys, dtype=np.float16), pa.float16()),
        _column(table, ROW_ID).cast(pa.int64()),
    ]
    names = ["x", "y", "rowid"]
    metadata = {
        "count": str(table.num_rows),
        "total": str(extent.total),
        "read": str(extent.read),
        "dropped": str(extent.read - table.num_rows),
        "sampled": "true" if extent.sampled else "false",
        "method": method.value,
    }
    if extent.cut:
        metadata["cut"] = extent.cut
    if method is ProjectionMethod.PCA:
        metadata["variance"] = f"{variance:.4f}"
    if colour is not None:
        channel, colour_kind = _colour(_column(table, "c"), colour.kind)
        arrays.append(channel)
        names.append("color")
        metadata["color_kind"] = colour_kind
    if label is not None:
        arrays.append(_dictionary(_text(_column(table, "l"))))
        names.append("label")
    schema = pa.schema([pa.field(n, a.type) for n, a in zip(names, arrays, strict=True)], metadata=metadata)
    return bounded(encode_arrow_stream(pa.table(arrays, schema=schema)))


# ── Graph ────────────────────────────────────────────────────────────────────────────────────────────────────────────


class GraphShape(StrEnum):
    #: Each row is an edge: from, to.
    EDGES = "edges"
    #: Each row is a node with an id, pointing at another row's id.
    SELF = "self"


class GraphSpec(Picks):
    """What a person picked for a graph."""

    shape: GraphShape
    #: The edge's from (``edges``) or the row's id (``self``).
    source: str = Field(min_length=1, max_length=256)
    #: The edge's to (``edges``) or the id the row points to (``self``).
    target: str = Field(min_length=1, max_length=256)
    relation: str | None = Field(default=None, min_length=1, max_length=256)
    weight: str | None = Field(default=None, min_length=1, max_length=256)
    #: Names the node the row's ``source`` names.
    label: str | None = Field(default=None, min_length=1, max_length=256)
    #: Colours the node the row's ``source`` names.
    group: str | None = Field(default=None, min_length=1, max_length=256)

    def structure_key(self) -> str:
        """What the expensive part of a graph depends on: the links, never what names or colours a node."""
        return self.model_dump_json(exclude_none=True, exclude={"version", "label", "group"})


class GraphDiagnostics(BaseModel):
    """What the rows read say about the graph, before anything was cut."""

    extent: ReadExtent
    #: Distinct nodes and distinct edges in the rows read.
    nodes: int
    edges: int
    #: Rows whose ``source`` or ``target`` is empty, or whose weight is no finite number, left out.
    null_rows: int
    dropped_weights: int = 0
    #: Rows pointing at themselves.
    self_loops: int
    #: Distinct ``target`` values that are no ``source`` value: an edge table's sinks, or a self-referencing table's
    #: dangling references.
    unmatched_targets: int
    #: The from and to columns hold different kinds of values (an integer 1 and the text "1" are different nodes).
    mixed_types: bool = False
    #: Ids or references longer than :data:`KEY_CHARS`, cut there (two that differ only past it meet).
    clipped_keys: int = 0
    #: Nodes and edges read and not drawn, the heaviest kept.
    cut_nodes: int
    cut_edges: int


class DerivedNode(BaseModel):
    """One node: its typed key, the name it is shown by, its group, its degree (or summed weight) among the edges
    drawn, and the rows that name it among the rows read (the rows its key lists)."""

    id: str
    name: str
    type: str = ""
    mentions: int
    #: How many rows name the node, at either end: the rows its key lists (:func:`graph_members`). Every node drawn is
    #: named by at least one.
    rows: int
    #: The first of those rows.
    first: int
    #: A row naming the node at its ``from`` (or ``id``) end, which its label and group describe; none for a node only
    #: ever pointed at, which keeps its own value as its name.
    labelled_by: int | None = None


class DerivedEdge(BaseModel):
    source: str
    target: str
    description: str = ""


class DerivedGraph(BaseModel):
    """Nodes and edges in the shape the Graph panel lays out, with what the read saw."""

    built: bool = True
    nodes: list[DerivedNode]
    edges: list[DerivedEdge]
    diagnostics: GraphDiagnostics


class _GraphRows(BaseModel):
    """A graph's rows as its structure and its members are both read: the typed keys and which rows count."""

    model_config = {"arbitrary_types_allowed": True}

    table: pa.Table
    extent: ReadExtent
    src: pa.Array
    dst: pa.Array
    #: The rows that are edges (``edges``) or nodes (``self``): what the counts and the members are over.
    counted: pa.Array
    complete: pa.Array
    null_rows: int
    dropped_weights: int
    clipped: int


class _GraphPicks(BaseModel):
    source: ColumnView
    target: ColumnView
    relation: ColumnView | None = None
    weight: ColumnView | None = None


def _graph_columns(columns: list[ColumnView], spec: GraphSpec) -> _GraphPicks:
    edges = spec.shape is GraphShape.EDGES
    picks = _GraphPicks(
        source=picked(columns, spec.source, _KEYS, "from" if edges else "id"),
        target=picked(columns, spec.target, _KEYS, "to" if edges else "points-to"),
        relation=picked(columns, spec.relation, _GROUPS, "relation") if spec.relation else None,
        weight=picked(columns, spec.weight, _NUMERIC, "weight") if spec.weight else None,
    )
    _apart(**{("from" if edges else "id"): picks.source, ("to" if edges else "points-to"): picks.target})
    return picks


def _graph_rows(ds: lance.LanceDataset, picks: _GraphPicks, spec: GraphSpec, cap: int, within: Budget) -> _GraphRows:
    """The one read a graph's structure and a node's rows are both made from, and which of its rows count."""
    source, target, relation, weight = picks.source, picks.target, picks.relation, picks.weight
    projection = {"s": _read_as(source, KEY_CHARS), "t": _read_as(target, KEY_CHARS)}
    if relation is not None:
        projection["r"] = _read_as(relation, LABEL_CHARS)
    if weight is not None:
        projection["w"] = _read_as(weight)
    table, extent = read_spread(ds, projection, cap, within=within)
    src, clipped_src = _keys(_column(table, "s"), source.kind)
    dst, clipped_dst = _keys(_column(table, "t"), target.kind)
    weighed = _finite(_column(table, "w")) if weight is not None else pa.array([True] * table.num_rows, pa.bool_())
    dropped = table.num_rows - _count(weighed) if weight is not None else 0
    if spec.shape is GraphShape.EDGES:
        present = _call("and", src.is_valid(), dst.is_valid())
        counted = _call("and", present, weighed)
        null_rows = table.num_rows - _count(present)
    else:
        counted = _call("and", src.is_valid(), weighed)
        null_rows = src.null_count
    complete = _call("and", _call("and", src.is_valid(), dst.is_valid()), weighed)
    return _GraphRows(
        table=table,
        extent=extent,
        src=src,
        dst=dst,
        counted=counted,
        complete=complete,
        null_rows=null_rows,
        dropped_weights=dropped,
        clipped=clipped_src + clipped_dst,
    )


def build_graph(ds: lance.LanceDataset, columns: list[ColumnView], spec: GraphSpec, *, within: Budget, cap: int | None = None) -> DerivedGraph:
    """The graph ``spec`` links, without its labels and groups (:func:`name_graph`): the rows read, grouped into distinct
    edges ranked by summed weight (or count), the :data:`GRAPH_EDGES` heaviest kept, then the :data:`GRAPH_NODES`
    best-connected nodes among them.

    Raises:
        ValidationError: A column the table lacks, one that holds values its role cannot read, or one column for both
            ends.
    """
    picks = _graph_columns(columns, spec)
    rows = _graph_rows(ds, picks, spec, cap or (STRUCTURE_ROWS if spec.shape is GraphShape.EDGES else NODE_ROWS), within)
    within.check()
    sources = rows.src.filter(rows.counted).unique()
    nodes_seen = sources if spec.shape is GraphShape.SELF else pa.concat_arrays([sources, rows.dst.filter(rows.complete).unique()]).unique()
    # A "to" is unmatched when no row names it as a "from", whatever else that row lacks.
    targets = rows.dst.filter(rows.complete).unique()
    named_sources = rows.src.drop_null().unique()
    unmatched = len(targets) - _count(_call("is_in", targets, options=pc.SetLookupOptions(value_set=named_sources)))

    edge_cols: dict[str, pa.Array] = {"s": rows.src.filter(rows.complete), "t": rows.dst.filter(rows.complete)}
    if picks.relation is not None:
        edge_cols["r"] = _text(_column(rows.table, "r")).filter(rows.complete)
    if picks.weight is not None:
        edge_cols["w"] = _column(rows.table, "w").cast(pa.float64()).filter(rows.complete)
    edges_table = pa.table(edge_cols)
    keys = ["s", "t", *(["r"] if picks.relation is not None else [])]
    aggregates: list[tuple[str, str]] = [("s", "count")]
    if picks.weight is not None:
        aggregates.append(("w", "sum"))
    grouped = edges_table.group_by(keys).aggregate(aggregates)
    within.check()
    rank = grouped.column("w_sum") if picks.weight is not None else grouped.column("s_count")
    ranked = grouped.append_column("rank", pc.fill_null(pc.cast(rank, pa.float64()), 0.0)).sort_by(
        [("rank", "descending"), ("s", "ascending"), ("t", "ascending")]
    )
    self_loops = _count(_call("equal", edges_table.column("s"), edges_table.column("t")))
    if spec.shape is GraphShape.SELF:
        # A reference to no row is counted (unmatched_targets) and not drawn: there is no node for it to point at.
        ranked = ranked.filter(_call("is_in", ranked.column("t"), options=pc.SetLookupOptions(value_set=sources)))
    kept = ranked.slice(0, GRAPH_EDGES).to_pylist()
    degree: dict[str, float] = defaultdict(float)
    for edge in kept:
        heft = float(edge["w_sum"] or 0.0) if picks.weight is not None else float(edge["s_count"])
        degree[edge["s"]] += heft
        degree[edge["t"]] += heft
    if spec.shape is GraphShape.SELF:
        # Every row is a node, an unreferenced root as much as a node pointing elsewhere.
        for node in sources.to_pylist()[:GRAPH_NODES]:
            degree.setdefault(node, 0.0)
    within.check()
    best = sorted(degree, key=lambda n: (-degree[n], n))[:GRAPH_NODES]
    shown = set(best)
    edges = [e for e in kept if e["s"] in shown and e["t"] in shown]
    keys = pa.array(best, pa.string())
    named = _naming_counts(rows, keys)
    from_end, to_end = _first_naming(rows, keys)
    return DerivedGraph(
        nodes=[
            DerivedNode(
                id=n,
                name=_shown(n)[:LABEL_CHARS],
                mentions=max(1, round(degree[n])),
                rows=named[n],
                first=min(r for r in (from_end[i], to_end[i]) if r is not None),
                labelled_by=from_end[i],
            )
            for i, n in enumerate(best)
        ],
        edges=[DerivedEdge(source=e["s"], target=e["t"], description=str(e.get("r") or "")) for e in edges],
        diagnostics=GraphDiagnostics(
            extent=rows.extent,
            nodes=len(nodes_seen),
            edges=ranked.num_rows,
            null_rows=rows.null_rows,
            dropped_weights=rows.dropped_weights,
            self_loops=self_loops,
            unmatched_targets=unmatched,
            mixed_types=picks.source.kind is not picks.target.kind,
            clipped_keys=rows.clipped,
            cut_nodes=max(0, len(nodes_seen) - len(best)),
            cut_edges=max(0, ranked.num_rows - len(edges)),
        ),
    )


def _naming(rows: _GraphRows, key: str) -> pa.Array:
    """Which rows name ``key``: the counted rows whose ``source`` or ``target`` is it. The one definition both a node's
    count and its rows read."""
    mine = _call("or_kleene", _call("equal", rows.src, pa.scalar(key)), _call("equal", rows.dst, pa.scalar(key)))
    return _call("and", _call("coalesce", mine, pa.scalar(value=False)), rows.counted)


def _first_naming(rows: _GraphRows, keys: pa.Array) -> tuple[list[int | None], list[int | None]]:
    """For each of ``keys``, the first counted row naming it at its ``from`` end and at its ``to`` end (``None`` where no
    row does): :func:`_naming`'s rows, by end."""
    rowids = _column(rows.table, ROW_ID).filter(rows.counted).to_pylist()
    ends: list[list[int | None]] = []
    for end in (rows.src, rows.dst):
        found = _call("index_in", keys, options=pc.SetLookupOptions(value_set=end.filter(rows.counted)))
        ends.append([None if i is None else int(rowids[i]) for i in found.to_pylist()])
    return ends[0], ends[1]


def _naming_counts(rows: _GraphRows, keys: pa.Array) -> dict[str, int]:
    """How many counted rows name each of ``keys`` (:func:`_naming`'s rows, for every node at once): a row counts once
    for each distinct key it names."""
    src = rows.src.filter(rows.counted)
    dst = rows.dst.filter(rows.counted)
    other = dst.filter(_call("coalesce", _call("not_equal", src, dst), pa.scalar(value=True)))
    counts: dict[str, int] = defaultdict(int)
    for part in (src, other):
        for item in part.filter(_call("is_in", part, options=pc.SetLookupOptions(value_set=keys))).value_counts().to_pylist():
            counts[item["values"]] += item["counts"]
    return counts


def name_graph(ds: lance.LanceDataset, columns: list[ColumnView], spec: GraphSpec, graph: DerivedGraph) -> DerivedGraph:
    """``graph`` with each node's label and group read from one row naming it as its source: the label and group columns
    are read for the nodes drawn alone, after the cut.

    Raises:
        ValidationError: A label or group column the table lacks or cannot read for its role.
    """
    label = picked(columns, spec.label, _LABELS, "label") if spec.label else None
    group = picked(columns, spec.group, _GROUPS, "colour") if spec.group else None
    rowids = sorted({n.labelled_by for n in graph.nodes if n.labelled_by is not None})
    if (label is None and group is None) or not rowids:
        return graph
    projection = {name: _read_as(column, LABEL_CHARS) for name, column in (("l", label), ("g", group)) if column is not None}
    found = ds.to_table(columns={**projection, ROW_ID: ROW_ID}, filter=f"{ROW_ID} IN ({', '.join(str(r) for r in rowids)})").to_pylist()
    by_row = {int(r[ROW_ID]): r for r in found}
    named: list[DerivedNode] = []
    for node in graph.nodes:
        row = by_row.get(node.labelled_by) if node.labelled_by is not None else None
        update: dict[str, object] = {}
        if row is not None and label is not None and row.get("l") is not None:
            update["name"] = str(row["l"])[:LABEL_CHARS]
        if row is not None and group is not None and row.get("g") is not None:
            update["type"] = str(row["g"])[:LABEL_CHARS]
        named.append(node.model_copy(update=update))
    return graph.model_copy(update={"nodes": named})


def graph_members(ds: lance.LanceDataset, columns: list[ColumnView], spec: GraphSpec, key: str, *, within: Budget) -> tuple[list[int], ReadExtent]:
    """The row ids of the rows that name the node ``key``, in the read its graph was drawn from (:func:`_naming`)."""
    picks = _graph_columns(columns, spec)
    rows = _graph_rows(ds, picks, spec, STRUCTURE_ROWS if spec.shape is GraphShape.EDGES else NODE_ROWS, within)
    within.check()
    return [int(r) for r in _column(rows.table, ROW_ID).filter(_naming(rows, key)).to_pylist()], rows.extent


# ── Tree ─────────────────────────────────────────────────────────────────────────────────────────────────────────────


class TreeShape(StrEnum):
    #: Each row is a node: an id and its parent's id.
    PARENT = "parent"
    #: Each row's place is one path, its levels joined by a separator.
    PATH = "path"


class TreeSpec(Picks):
    """What a person picked for a tree."""

    shape: TreeShape
    #: The row's id (``parent``) or its path (``path``).
    node: str = Field(min_length=1, max_length=256)
    #: The parent's id (``parent`` only).
    parent: str | None = Field(default=None, min_length=1, max_length=256)
    #: The separator between a path's levels (``path`` only).
    separator: str = Field(default="/", min_length=1, max_length=8)
    #: Names a node (``parent`` only; a path's levels name themselves).
    label: str | None = Field(default=None, min_length=1, max_length=256)
    #: A number each row adds to its node's area; unset, each row counts one.
    size: str | None = Field(default=None, min_length=1, max_length=256)

    def structure_key(self) -> str:
        return self.model_dump_json(exclude_none=True, exclude={"version", "label"})


class TreeNode(BaseModel):
    """One node of the tree, in the shape the treemap draws: a branch's area is its leaves' ``value``s.

    ``id`` is the node's key, which a node's rows are asked for by (:func:`tree_members`); ``rows`` is how many rows
    that key lists: the rows at or below the node (a "(itself)" leaf: the rows at the node alone). A node standing for
    branches the read left out has no key."""

    name: str
    id: str | None = None
    value: float | None = None
    rows: int
    #: A row of the node, which its label is read from.
    first: int | None = None
    children: list[TreeNode] | None = None


class TreeDiagnostics(BaseModel):
    extent: ReadExtent
    #: Distinct nodes in the rows read.
    nodes: int
    roots: int
    #: Nodes whose parent names no node read: shown at the top.
    orphans: int
    #: Nodes on a cycle, or below one, which no root reaches: left out.
    cycles: int
    #: Rows with no id or no path, or whose size is no finite number, left out.
    null_rows: int
    dropped_sizes: int = 0
    #: Ids, references or paths longer than the read takes, cut there.
    clipped: int = 0
    depth: int
    #: Nodes read and not drawn: past the deepest level, or among a node's smallest children (each node shows its
    #: largest :data:`TREE_CHILDREN`, the rest as one "more" leaf).
    cut_nodes: int


class DerivedTree(BaseModel):
    built: bool = True
    hierarchy: TreeNode
    diagnostics: TreeDiagnostics
    #: The treemap's unclustered-bucket name; a derived tree has none.
    noise_label: str = ""


class TreeKey(BaseModel):
    """A tree node's key: a path's levels or an id's typed key, and whether it is the node alone ("(itself)")."""

    path: list[str] | None = Field(default=None, max_length=TREE_DEPTH)
    id: str | None = Field(default=None, max_length=KEY_CHARS + 2)
    alone: bool = False

    def encoded(self) -> str:
        return json.dumps(self.model_dump(exclude_none=True, exclude_defaults=True), ensure_ascii=False, separators=(",", ":"))


def _levels(path: str, separator: str) -> tuple[str, ...]:
    """A path's levels as a tree reads them: split at ``separator``, empty levels dropped (a leading, trailing or doubled
    separator adds none), each cut to :data:`SEGMENT_CHARS`, at most :data:`TREE_DEPTH` of them. The one definition the
    tree's nodes and a node's rows are both read by."""
    levels: list[str] = []
    for level in path.split(separator):
        if level:
            levels.append(level[:SEGMENT_CHARS])
            if len(levels) == TREE_DEPTH:
                break
    return tuple(levels)


class _Shaper:
    """Turns ``children`` under the roots into :class:`TreeNode`s within the tree's bounds, counting what it leaves out."""

    def __init__(
        self, children: dict[str, list[str]], own: dict[str, float], own_rows: dict[str, int], names: dict[str, str], keys: dict[str, TreeKey]
    ) -> None:
        self.children = children
        self.own = own
        self.own_rows = own_rows
        self.names = names
        self.keys = keys
        self.total: dict[str, float] = {}
        self.rows: dict[str, int] = {}
        self.shown = 0
        self.cut = 0
        self.depth = 0

    def weigh(self, roots: list[str]) -> None:
        """Every node's area and rows: its own and its subtree's, iteratively so a deep tree does not exhaust the stack."""
        stack: list[tuple[str, bool]] = [(r, False) for r in roots]
        while stack:
            node, done = stack.pop()
            if done:
                kids = self.children.get(node, [])
                self.total[node] = self.own.get(node, 0.0) + sum(self.total.get(c, 0.0) for c in kids)
                self.rows[node] = self.own_rows.get(node, 0) + sum(self.rows.get(c, 0) for c in kids)
                continue
            stack.append((node, True))
            stack.extend((c, False) for c in self.children.get(node, []) if c not in self.total)

    def size_of(self, node: str) -> int:
        count, stack = 0, [node]
        while stack:
            count += 1
            stack.extend(self.children.get(stack.pop(), []))
        return count

    def shape(self, node: str, depth: int) -> TreeNode:
        self.shown += 1
        self.depth = max(self.depth, depth)
        kids = sorted(self.children.get(node, []), key=lambda c: (-self.total.get(c, 0.0), c))
        name = self.names.get(node, node)
        key = self.keys[node].encoded()
        if not kids:
            return TreeNode(name=name, id=key, value=self.own.get(node, 0.0), rows=self.rows.get(node, 0))
        if depth >= TREE_DEPTH or self.shown >= TREE_NODES:
            self.cut += self.size_of(node) - 1
            return TreeNode(name=name, id=key, value=self.total.get(node, 0.0), rows=self.rows.get(node, 0))
        out = [self.shape(c, depth + 1) for c in kids[:TREE_CHILDREN] if self.shown < TREE_NODES]
        rest = kids[len(out) :]
        if rest:
            self.cut += sum(self.size_of(c) for c in rest)
            out.append(TreeNode(name=f"{len(rest):,} more", value=sum(self.total.get(c, 0.0) for c in rest), rows=sum(self.rows.get(c, 0) for c in rest)))
        if self.own_rows.get(node, 0) > 0:
            alone = self.keys[node].model_copy(update={"alone": True}).encoded()
            out.append(TreeNode(name=f"{name} (itself)", id=alone, value=self.own.get(node, 0.0), rows=self.own_rows[node]))
        return TreeNode(name=name, id=key, rows=self.rows.get(node, 0), children=out)


def _top(shaper: _Shaper, roots: list[str]) -> TreeNode:
    """The tree's one root over ``roots``: the largest :data:`TREE_CHILDREN` of them, the rest as one "more" leaf."""
    ordered = sorted(roots, key=lambda r: (-shaper.total.get(r, 0.0), r))
    children: list[TreeNode] = []
    for root in ordered[:TREE_CHILDREN]:
        if shaper.shown >= TREE_NODES:
            break
        children.append(shaper.shape(root, 1))
    rest = ordered[len(children) :]
    if rest:
        shaper.cut += sum(shaper.size_of(r) for r in rest)
        children.append(TreeNode(name=f"{len(rest):,} more", value=sum(shaper.total.get(r, 0.0) for r in rest), rows=sum(shaper.rows.get(r, 0) for r in rest)))
    return TreeNode(name="All rows", rows=sum(shaper.rows.get(r, 0) for r in roots), children=children)


class _TreeRows(BaseModel):
    """A tree's rows as its structure and its members are both read: each row's node, its amount, and whether it counts."""

    model_config = {"arbitrary_types_allowed": True}

    extent: ReadExtent
    rowids: list[int]
    #: Each row's node: its path's levels, or its typed id (``None``: the row is left out).
    nodes: list[tuple[str, ...] | str | None]
    parents: list[str | None]
    amounts: list[float]
    null_rows: int
    dropped_sizes: int
    clipped: int


class _TreePicks(BaseModel):
    node: ColumnView
    parent: ColumnView | None = None
    size: ColumnView | None = None


def _tree_columns(columns: list[ColumnView], spec: TreeSpec) -> _TreePicks:
    size = picked(columns, spec.size, _NUMERIC, "size") if spec.size else None
    if spec.shape is TreeShape.PATH:
        return _TreePicks(node=picked(columns, spec.node, frozenset({ColumnKind.TEXT}), "path"), size=size)
    if spec.parent is None:
        raise ValidationError("a parent tree needs the column that names each row's parent")
    node = picked(columns, spec.node, _KEYS, "id")
    parent = picked(columns, spec.parent, _KEYS, "parent")
    _apart(id=node, parent=parent)
    return _TreePicks(node=node, parent=parent, size=size)


def _tree_rows(ds: lance.LanceDataset, picks: _TreePicks, spec: TreeSpec, within: Budget, cap: int | None = None) -> _TreeRows:
    """The one read a tree's structure and a node's rows are both made from, and each row's node."""
    node, parent, size = picks.node, picks.parent, picks.size
    path_shape = spec.shape is TreeShape.PATH
    projection = {"n": _read_as(node, PATH_CHARS if path_shape else KEY_CHARS)}
    if parent is not None:
        projection["p"] = _read_as(parent, KEY_CHARS)
    if size is not None:
        projection["z"] = _read_as(size)
    table, extent = read_spread(ds, projection, cap or (STRUCTURE_ROWS if path_shape else NODE_ROWS), within=within)
    within.check()
    sized = _finite(_column(table, "z")) if size is not None else pa.array([True] * table.num_rows, pa.bool_())
    dropped = table.num_rows - _count(sized) if size is not None else 0
    amounts = _column(table, "z").cast(pa.float64()).to_pylist() if size is not None else [1.0] * table.num_rows
    raw = _column(table, "n")
    nodes: list[tuple[str, ...] | str | None]
    parents: list[str | None] = [None] * table.num_rows
    if path_shape or parent is None:
        text = raw.cast(pa.string())
        clipped = _count(_call("greater", _call("utf8_length", text), pa.scalar(PATH_CHARS)))
        cache: dict[str, tuple[str, ...]] = {}
        nodes = []
        for i, value in enumerate(_text(text, PATH_CHARS).to_pylist()):
            if i % CHECK_EVERY == 0:
                within.check()
            if value is None:
                nodes.append(None)
                continue
            levels = cache.get(value)
            if levels is None:
                levels = cache[value] = _levels(value, spec.separator)
            nodes.append(levels or None)
    else:
        ids, clipped_ids = _keys(raw, node.kind)
        refs, clipped_refs = _keys(_column(table, "p"), parent.kind)
        clipped = clipped_ids + clipped_refs
        nodes = list(ids.to_pylist())
        parents = list(refs.to_pylist())
    sized_list = sized.to_pylist()
    null_rows = sum(1 for n in nodes if n is None)
    for i, ok in enumerate(sized_list):
        if not ok:
            nodes[i] = None
    return _TreeRows(
        extent=extent,
        rowids=[int(r) for r in _column(table, ROW_ID).to_pylist()],
        nodes=nodes,
        parents=parents,
        amounts=[float(a) if ok else 0.0 for a, ok in zip(amounts, sized_list, strict=True)],
        null_rows=null_rows,
        dropped_sizes=dropped,
        clipped=clipped,
    )


class _Structure(BaseModel):
    """A tree's nodes keyed by name in its own terms (a path joined, or a typed id), with each one's key."""

    children: dict[str, list[str]]
    roots: list[str]
    own: dict[str, float]
    own_rows: dict[str, int]
    keys: dict[str, TreeKey]
    names: dict[str, str]
    first: dict[str, int]
    orphans: int = 0
    cycles: int = 0
    #: Each row's node in the structure's terms, or ``None`` for a row left out.
    row_node: list[str | None]


_JOIN: Final = "\x00"


def _structure(rows: _TreeRows, spec: TreeSpec, within: Budget) -> _Structure:
    own: dict[str, float] = defaultdict(float)
    own_rows: dict[str, int] = defaultdict(int)
    keys: dict[str, TreeKey] = {}
    names: dict[str, str] = {}
    first: dict[str, int] = {}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    row_node: list[str | None] = []
    if spec.shape is TreeShape.PATH:
        for i, levels in enumerate(rows.nodes):
            if i % CHECK_EVERY == 0:
                within.check()
            if not isinstance(levels, tuple):
                row_node.append(None)
                continue
            for depth in range(1, len(levels) + 1):
                here = _JOIN.join(levels[:depth])
                if here not in keys:
                    keys[here] = TreeKey(path=list(levels[:depth]))
                    names[here] = levels[depth - 1]
                    (children[_JOIN.join(levels[: depth - 1])] if depth > 1 else roots).append(here)
            leaf = _JOIN.join(levels)
            own[leaf] += rows.amounts[i]
            own_rows[leaf] += 1
            first.setdefault(leaf, rows.rowids[i])
            row_node.append(leaf)
        return _Structure(children=children, roots=roots, own=own, own_rows=own_rows, keys=keys, names=names, first=first, row_node=row_node)
    parent_of: dict[str, str | None] = {}
    for i, node in enumerate(rows.nodes):
        if i % CHECK_EVERY == 0:
            within.check()
        if not isinstance(node, str):
            row_node.append(None)
            continue
        own[node] += rows.amounts[i]
        own_rows[node] += 1
        reference = rows.parents[i]
        parent_of.setdefault(node, reference if reference != node else None)
        first.setdefault(node, rows.rowids[i])
        names.setdefault(node, _shown(node))
        keys.setdefault(node, TreeKey(id=node))
        row_node.append(node)
    orphans = 0
    for node, reference in parent_of.items():
        if reference is None:
            roots.append(node)
        elif reference in parent_of:
            children[reference].append(node)
        else:
            orphans += 1
            roots.append(node)
    reached: set[str] = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n in reached:
            continue
        reached.add(n)
        stack.extend(children.get(n, []))
    cycles = len(parent_of) - len(reached)
    # A node on a cycle is drawn nowhere, so its rows are no node's rows either.
    row_node = [n if n is None or n in reached else None for n in row_node]
    return _Structure(
        children=children,
        roots=roots,
        own=own,
        own_rows=own_rows,
        keys=keys,
        names=names,
        first=first,
        orphans=orphans,
        cycles=cycles,
        row_node=row_node,
    )


def build_tree(ds: lance.LanceDataset, columns: list[ColumnView], spec: TreeSpec, *, within: Budget, cap: int | None = None) -> DerivedTree:
    """The tree ``spec`` nests, without its labels (:func:`name_tree`), with its orphans, cycles and cuts counted.

    Raises:
        ValidationError: A column the table lacks, one its role cannot read, one column for both id and parent, or a
            parent missing from a parent tree.
    """
    picks = _tree_columns(columns, spec)
    rows = _tree_rows(ds, picks, spec, within, cap)
    shaped = _structure(rows, spec, within)
    within.check()
    shaper = _Shaper(shaped.children, shaped.own, shaped.own_rows, shaped.names, shaped.keys)
    shaper.weigh(shaped.roots)
    top = _top(shaper, shaped.roots)
    within.check()
    if spec.shape is TreeShape.PARENT:
        _mark_first(top, shaped)
    return DerivedTree(
        hierarchy=top,
        diagnostics=TreeDiagnostics(
            extent=rows.extent,
            nodes=len(shaped.keys),
            roots=len(shaped.roots) - shaped.orphans,
            orphans=shaped.orphans,
            cycles=shaped.cycles,
            null_rows=rows.null_rows,
            dropped_sizes=rows.dropped_sizes,
            clipped=rows.clipped,
            depth=shaper.depth,
            cut_nodes=shaper.cut,
        ),
    )


def _mark_first(node: TreeNode, shaped: _Structure) -> None:
    """Note on each drawn node of a parent tree one of its rows, which its label is read from."""
    stack = [node]
    while stack:
        here = stack.pop()
        if here.id is not None:
            key = TreeKey.model_validate_json(here.id)
            if key.id is not None:
                here.first = shaped.first.get(key.id)
        stack.extend(here.children or [])


def name_tree(ds: lance.LanceDataset, columns: list[ColumnView], spec: TreeSpec, tree: DerivedTree) -> DerivedTree:
    """``tree`` with each drawn node of a parent tree named by its label column, read for the nodes drawn alone.

    Raises:
        ValidationError: A label column the table lacks or cannot read.
    """
    if spec.shape is TreeShape.PATH or spec.label is None:
        return tree
    label = picked(columns, spec.label, _LABELS, "label")
    nodes: list[TreeNode] = []
    stack = [tree.hierarchy]
    while stack:
        here = stack.pop()
        nodes.append(here)
        stack.extend(here.children or [])
    rowids = sorted({n.first for n in nodes if n.first is not None})
    if not rowids:
        return tree
    found = ds.to_table(columns={"l": _read_as(label, LABEL_CHARS), ROW_ID: ROW_ID}, filter=f"{ROW_ID} IN ({', '.join(str(r) for r in rowids)})")
    by_row = {int(r[ROW_ID]): r["l"] for r in found.to_pylist()}
    for here in nodes:
        name = by_row.get(here.first) if here.first is not None else None
        if name is not None:
            here.name = f"{str(name)[:LABEL_CHARS]} (itself)" if here.name.endswith(" (itself)") else str(name)[:LABEL_CHARS]
    return tree


def tree_members(ds: lance.LanceDataset, columns: list[ColumnView], spec: TreeSpec, key: TreeKey, *, within: Budget) -> tuple[list[int], ReadExtent]:
    """The row ids of the rows the node ``key`` stands for, in the read its tree was drawn from: the rows at or below the
    node (the node alone where ``key.alone``), by the same structure the tree counted them by.

    Raises:
        ValidationError: The key names no node of this kind of tree.
    """
    picks = _tree_columns(columns, spec)
    rows = _tree_rows(ds, picks, spec, within)
    shaped = _structure(rows, spec, within)
    within.check()
    if spec.shape is TreeShape.PATH:
        if key.path is None:
            raise ValidationError("a path tree's node is named by its levels")
        target = _JOIN.join(key.path)
    else:
        if key.id is None:
            raise ValidationError("a parent tree's node is named by its id")
        target = key.id
    if key.alone:
        wanted = {target}
    else:
        wanted, stack = set(), [target]
        while stack:
            here = stack.pop()
            if here in wanted:
                continue
            wanted.add(here)
            stack.extend(shaped.children.get(here, []))
    return [rowid for rowid, node in zip(rows.rowids, shaped.row_node, strict=True) if node in wanted], rows.extent


# ── A node's rows ────────────────────────────────────────────────────────────────────────────────────────────────


class NodeRows(RowsPage):
    """One page of the rows a graph's or a tree's node stands for, with the read they were decided over (a sample of a
    table past the read's cap: the same rows the node was counted over)."""

    extent: ReadExtent


class GraphNodeQuery(GraphSpec):
    """A graph's set-up, one of its nodes by key, and which page of the rows naming it."""

    key: str = Field(min_length=3, max_length=KEY_CHARS + 2)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)


class TreeNodeQuery(TreeSpec):
    """A tree's set-up, one of its nodes by key (a :class:`TreeKey` as JSON), and which page of its rows."""

    key: str = Field(min_length=2, max_length=4 * KEY_CHARS + TREE_DEPTH * (SEGMENT_CHARS + 8))
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)

    def tree_key(self) -> TreeKey:
        """The node this query names.

        Raises:
            ValidationError: The key is not a tree node's key.
        """
        try:
            return TreeKey.model_validate_json(self.key)
        except ValueError as exc:
            raise ValidationError("this is not a tree node's key") from exc


# ── Profile ──────────────────────────────────────────────────────────────────────────────────────────────────────────


class ColumnProfile(BaseModel):
    """What a sample says of one column: enough for the set-up forms to suggest, never to decide."""

    name: str
    kind: ColumnKind
    dim: int | None = None
    #: Distinct values among the sample's, at most :data:`PROFILE_DISTINCT` (``distinct_capped`` says it stopped).
    distinct: int | None = None
    distinct_capped: bool = False
    nulls: int = 0
    #: For a text column: the separator most of its values hold, and the share that hold it.
    separator: str | None = None
    separated: float = 0.0
    #: A few of its values, cut to :data:`LABEL_CHARS`.
    examples: list[str] = Field(default_factory=list)
    #: Other columns of a key kind whose sampled values this column's values are found among, and the share found:
    #: what a parent or a points-to column looks like beside its id.
    refers_to: dict[str, float] = Field(default_factory=dict)
    #: Whether a vector column can place an atlas (its width within :data:`VECTOR_MAX_DIM`).
    projectable: bool = False


class TableProfile(BaseModel):
    table: str
    version: int
    extent: ReadExtent
    columns: list[ColumnProfile]


def _separator(values: list[str]) -> tuple[str | None, float]:
    if not values:
        return None, 0.0
    best: tuple[str | None, float] = (None, 0.0)
    for sep in SEPARATORS:
        share = sum(1 for v in values if sep in v.strip(sep)) / len(values)
        if share > best[1]:
            best = (sep, share)
    return best


def profile_table(ds: lance.LanceDataset, columns: list[ColumnView], table_name: str, *, within: Budget) -> TableProfile:
    """A sample's view of every column the set-up forms could offer: its kind, distinct values, nulls, a path's
    separator and which key columns it seems to refer to. Only the kinds a set-up weighs are read (text cut at
    :data:`LABEL_CHARS`); a vector is described by its width, which the schema holds."""
    readable = [c for c in columns if c.kind in _PROFILED]
    projection = {f"c{i}": _read_as(c, LABEL_CHARS) for i, c in enumerate(readable)}
    table, extent = read_spread(ds, projection, PROFILE_ROWS, within=within)
    samples: dict[str, set[str]] = {}
    profiles: list[ColumnProfile] = []
    position = {c.name: f"c{i}" for i, c in enumerate(readable)}
    for column in columns:
        profile = ColumnProfile(name=column.name, kind=column.kind, dim=column.dim)
        if column.kind is ColumnKind.VECTOR:
            profile.projectable = 2 <= (column.dim or 0) <= VECTOR_MAX_DIM
        if column.name in position:
            values = _column(table, position[column.name])
            profile.nulls = values.null_count
            text = [v for v in _text(values).to_pylist() if v is not None]
            distinct = set(text)
            profile.distinct = min(len(distinct), PROFILE_DISTINCT)
            profile.distinct_capped = len(distinct) > PROFILE_DISTINCT
            profile.examples = sorted(distinct)[:3]
            if column.kind is ColumnKind.TEXT:
                profile.separator, profile.separated = _separator(text)
            if column.kind in _KEYS and len(samples) < PROFILE_KEY_COLUMNS:
                samples[column.name] = distinct
        profiles.append(profile)
    for profile in profiles:
        mine = samples.get(profile.name)
        if not mine:
            continue
        for other, theirs in samples.items():
            if other == profile.name or not theirs:
                continue
            found = len(mine & theirs) / len(mine)
            if found > 0:
                profile.refers_to[other] = round(found, 3)
    return TableProfile(table=table_name, version=int(ds.version), extent=extent, columns=profiles)


__all__ = [
    "ANSWER_BYTES",
    "VECTOR_MAX_DIM",
    "Abandoned",
    "AtlasSpec",
    "Budget",
    "DerivedGraph",
    "DerivedTree",
    "GraphNodeQuery",
    "GraphShape",
    "GraphSpec",
    "NodeRows",
    "Picks",
    "ReadExtent",
    "TableProfile",
    "TreeKey",
    "TreeNodeQuery",
    "TreeShape",
    "TreeSpec",
    "budget",
    "build_atlas",
    "build_graph",
    "build_tree",
    "graph_members",
    "name_graph",
    "name_tree",
    "pca_2d",
    "picked",
    "profile_table",
    "read_spread",
    "strict_json",
    "tree_json",
    "tree_members",
]
