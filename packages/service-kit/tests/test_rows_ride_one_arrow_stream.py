"""The rows encoder's branches no door's rows reach: a column mixing kinds, and a key only a later row has.

The scalar kinds, an all-null column and a nested column are pinned through the viewer's selection door
(`services/viewer/tests/test_a_selection_is_one_arrow_stream_of_rows.py`).
"""

from __future__ import annotations

import pyarrow as pa

from service_kit.arrow_rows import rows_to_arrow_stream


def test_a_mixed_column_is_json_text_and_a_later_rows_key_is_a_column() -> None:
    table = pa.ipc.open_stream(rows_to_arrow_stream([{"a": "x"}, {"a": 1, "b": True}])).read_all()

    assert {field.name: field.type for field in table.schema} == {"a": pa.string(), "b": pa.bool_()}
    assert table.schema.metadata == {b"rows": b"2", b"json_columns": b'["a"]'}
    assert table.to_pylist() == [{"a": '"x"', "b": None}, {"a": "1", "b": True}]
