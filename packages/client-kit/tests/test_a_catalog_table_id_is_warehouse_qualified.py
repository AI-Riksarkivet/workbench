"""A media table's catalog id names its warehouse first: ``warehouse$namespace$table`` (`docs/rask/COMMIT-EVENTS.md`).

rask refuses a table id of fewer than three segments with 400, and a one-segment namespace names a warehouse, so an id
that cannot be three segments is refused before a request is sent, and a configured default dataset when the settings
load. The settings name the one catalog the media plane reads, and refuse to load naming none or two.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from client_kit.media.config import Settings


def _settings(**env: object) -> Settings:
    return Settings.model_validate({"MEDIA_CATALOG_DIR": "/catalog", **env})


@pytest.mark.parametrize(
    ("env", "dataset", "table", "expected"),
    [
        pytest.param({}, "acme$bronze$scans", "pages", ["acme", "bronze", "scans", "pages"], id="nested-namespace"),
    ],
)
def test_catalog_table_id_puts_the_namespace_segments_before_the_table(env: dict[str, str], dataset: str, table: str, expected: list[str]) -> None:
    assert _settings(**env).catalog_table_id(dataset, table) == expected


@pytest.mark.parametrize(
    ("dataset", "table", "error"),
    [
        pytest.param("bronze", "pages", "not warehouse-qualified", id="one-segment-namespace"),
        pytest.param("acme$", "pages", "not warehouse-qualified", id="empty-segment"),
        pytest.param("acme$bronze", "a$b", "one non-empty segment", id="delimiter-in-table"),
        pytest.param("acme$bronze", "", "one non-empty segment", id="empty-table"),
    ],
)
def test_catalog_table_id_refuses_an_id_rask_would_refuse(dataset: str, table: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _settings().catalog_table_id(dataset, table)


def test_a_configured_default_dataset_rask_would_refuse_fails_the_settings() -> None:
    with pytest.raises(ValidationError, match="not warehouse-qualified"):
        _settings(MEDIA_DEFAULT_DATASET="bronze")


@pytest.mark.parametrize(
    "catalogs",
    [
        pytest.param({}, id="none"),
        pytest.param({"MEDIA_CATALOG_URI": "http://catalog:2333", "MEDIA_CATALOG_DIR": "/catalog"}, id="both"),
    ],
)
def test_the_settings_refuse_to_load_without_exactly_one_catalog(catalogs: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    with pytest.raises(ValidationError, match="set exactly one of MEDIA_CATALOG_URI"):
        Settings.model_validate(catalogs)
