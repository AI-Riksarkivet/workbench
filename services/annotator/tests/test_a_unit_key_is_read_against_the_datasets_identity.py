"""A unit key names one row by the dataset's declared identity: one segment per identity field, each of its column's type.

`annotator.annotations.unit.parse_unit` over a descriptor built as the viewer serves one. The doors' own wiring (a key
that does not parse is a 422 at the save door) is pinned in `test_a_projects_roles_are_grants_in_the_workbench_store.py`.
"""

from __future__ import annotations

import pytest

from annotator.annotations.unit import UnitKeyError, parse_unit
from client_kit.lancekit.descriptor import DatasetDescriptor
from client_kit.lancekit.introspect import ColumnInfo, TableInfo


def _dataset(fields: dict[str, str]) -> DatasetDescriptor:
    """A dataset whose rows table carries `fields` (name -> arrow type), its identity in that order."""
    columns = [ColumnInfo(name=name, arrow_type=arrow_type, nullable=False) for name, arrow_type in fields.items()]
    return DatasetDescriptor.model_validate(
        {
            "id": "acme$media",
            "tables": {"rows": TableInfo(name="rows", row_count=1, version=1, columns=columns, indexes=[])},
            "declared": {"identity": {"key_fields": list(fields)}, "search": {"row_table": "rows"}},
        }
    )


@pytest.mark.parametrize(
    ("fields", "key", "values"),
    [
        pytest.param({"doc_id": "string"}, "letter-001", {"doc_id": "letter-001"}, id="a-doc-only-identity"),
        pytest.param({"doc_id": "string", "page": "int64"}, "letter-001/3", {"doc_id": "letter-001", "page": 3}, id="an-integer-field"),
        pytest.param({"doc_id": "string", "side": "string"}, "letter-001/recto", {"doc_id": "letter-001", "side": "recto"}, id="a-string-field"),
    ],
)
def test_a_key_of_the_identitys_shape_names_its_typed_values(fields: dict[str, str], key: str, values: dict[str, str | int]) -> None:
    assert parse_unit(_dataset(fields), key).values == values


@pytest.mark.parametrize(
    ("fields", "key", "detail"),
    [
        pytest.param(
            {"doc_id": "string", "page": "int64"}, "letter-001/three", "unit key 'letter-001/three': page is an integer, not 'three'", id="not-an-integer"
        ),
        pytest.param(
            {"doc_id": "string", "at": "timestamp[us]"},
            "letter-001/1",
            "unit key 'letter-001/1': at is timestamp[us], which a unit key cannot name",
            id="an-unnameable-type",
        ),
    ],
)
def test_a_key_the_identity_cannot_name_is_refused_in_plain_words(fields: dict[str, str], key: str, detail: str) -> None:
    with pytest.raises(UnitKeyError) as refused:
        parse_unit(_dataset(fields), key)

    assert (refused.value.status_code, refused.value.detail) == (422, detail)
