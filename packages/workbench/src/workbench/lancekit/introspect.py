"""Runtime table/schema/index discovery — the *discovered* half of the descriptor.

Everything here is mechanical fact read from an opened Lance table (LANCE_MEDIA_MERGE §4.2): vector columns are
FixedSizeList<float> (dim = pyarrow ``list_size``), blob columns carry the ``lance.blob.v2`` extension, and indexes come
from ``ds.describe_indices()``. Which tables a dataset has is the catalog's answer (ListTables), read by the registry. No
corpus knowledge lives here.
"""

from __future__ import annotations

import lance
import pyarrow as pa
from pydantic import BaseModel

from workbench.lancekit.blobs import is_blob_field


class ColumnInfo(BaseModel):
    name: str
    arrow_type: str
    nullable: bool
    vector_dim: int | None = None
    is_blob: bool = False


class IndexInfo(BaseModel):
    name: str
    index_type: str
    columns: list[str]


class TableInfo(BaseModel):
    name: str
    row_count: int
    version: int
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)


def _column_info(field: pa.Field) -> ColumnInfo:
    dim: int | None = None
    if pa.types.is_fixed_size_list(field.type) and pa.types.is_floating(field.type.value_type):
        dim = field.type.list_size
    return ColumnInfo(
        name=field.name,
        arrow_type=str(field.type),
        nullable=field.nullable,
        vector_dim=dim,
        is_blob=is_blob_field(field),
    )


def _index_type(type_url: str) -> str:
    """Index kind from the proto type_url tail (`/lance.table.BTreeIndexDetails` → `BTree`).

    Vector indexes collapse to the generic `Vector` (the new API no longer names the
    algorithm, e.g. IVF_PQ) — display-only downstream, validated as an opaque string.
    """
    return type_url.rsplit(".", 1)[-1].removesuffix("IndexDetails") or "?"


def dataset_info(ds: lance.LanceDataset, name: str) -> TableInfo:
    """Introspect one already-opened Lance table, however it was opened (a URI, or a namespace on a vended credential)."""
    indexes = [IndexInfo(name=ix.name, index_type=_index_type(ix.type_url), columns=list(ix.field_names)) for ix in ds.describe_indices()]
    return TableInfo(
        name=name,
        row_count=ds.count_rows(),
        version=ds.version,
        columns=[_column_info(f) for f in ds.schema],
        indexes=indexes,
    )
