"""A unit's frame: the picture of one chunk of a dataset, found in its frames table.

A dataset declares its frames with the ``frames`` capability (``<table>.<blob column>``). The viewer serves a frame to a
person and signs a link to it; the annotator reads the same cell to show a model the picture a person labels. Both
resolve it here, so the picture a model is sent is the one the canvas draws.
"""

from __future__ import annotations

import lance
from pydantic import BaseModel, ConfigDict

from workbench.exceptions import NotFoundError
from workbench.lancekit.keys import chunk_key_filter, validate_doc_key
from workbench.lancekit.registry import DatasetHandle, table_dataset


#: The frame-index column of a frames table. A reserved contract name (like
#: ``_rowid``), not corpus knowledge: frame keys are identity.key_fields plus
#: this column, and it is matched in Python (see chunk_frame) per the planner bug.
FRAME_INDEX_COLUMN = "frame_idx"


def _frames_binding(handle: DatasetHandle) -> tuple[str, str, str | None] | None:
    """(table, blob column, mime column) of the per-chunk frames, or None.

    The table comes from the ``visual`` vector binding when declared (frames
    and frame embeddings share a table), else from the ``frames`` capability's
    table part; the blob column is always the capability's column part. The
    mime column is discovered: the first ``*_mime`` string column beside the
    blob (None → the JPEG default applies).
    """
    declared = handle.declared
    target = declared.capabilities.get("frames")
    if not target:
        return None
    cap_table, _, blob_column = target.partition(".")
    if not blob_column:
        return None
    table = cap_table
    if declared.search is not None and "visual" in declared.search.vectors:
        table = declared.search.vectors["visual"].table
    info = handle.descriptor.tables.get(table)
    if info is None or info.column(blob_column) is None:
        return None
    mime_column = next((c.name for c in info.columns if c.name.endswith("_mime") and not c.is_blob), None)
    return table, blob_column, mime_column


class FrameRow(BaseModel):
    """One chunk's frame: the frames table opened as the caller, its blob column, the frame's stable row id and its mime."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: str
    ds: lance.LanceDataset
    column: str
    row: int
    mime: str


def frame_row(handle: DatasetHandle, doc_id: str, keys: tuple[int, ...], frame_idx: int) -> FrameRow:
    """Resolve a chunk's ``frame_idx``-th frame (0 is the representative one) in the frames table; 404 until extracted.

    The key fields after the doc key map positionally onto ``keys``.
    """
    declared = handle.declared
    doc_id = validate_doc_key(declared, doc_id)
    binding = _frames_binding(handle)
    if binding is None:
        raise NotFoundError("frame not extracted yet")
    table, blob_column, mime_column = binding
    ds = table_dataset(handle, table)

    # frame_idx is matched in Python, NOT in the SQL predicate. With a scalar
    # (BTREE) index on the frames table's doc key, the combination of
    # `with_row_id=True` + a multi-clause filter that *also* constrains
    # frame_idx trips a Lance planner bug that silently returns 0 rows (the row
    # is there — verified via a filter without the frame_idx clause). Keying on
    # the identity fields in SQL and selecting frame_idx here sidesteps it; a
    # chunk has only a handful of frames, so the extra rows are negligible.
    columns = [FRAME_INDEX_COLUMN] + ([mime_column] if mime_column else [])
    keyed = ds.to_table(columns=columns, filter=chunk_key_filter(declared, doc_id, keys), with_row_id=True)
    row = next(
        (i for i in range(keyed.num_rows) if keyed.column(FRAME_INDEX_COLUMN)[i].as_py() == int(frame_idx)),
        None,
    )
    if row is None:
        raise NotFoundError("frame not extracted yet")
    mime = "image/jpeg"
    if mime_column is not None:
        mime_cell = keyed.column(mime_column)[row]
        mime = mime_cell.as_py() if mime_cell.is_valid else "image/jpeg"
    return FrameRow(table=table, ds=ds, column=blob_column, row=int(keyed.column("_rowid")[row].as_py()), mime=mime)


__all__ = ["FRAME_INDEX_COLUMN", "FrameRow", "frame_row"]
