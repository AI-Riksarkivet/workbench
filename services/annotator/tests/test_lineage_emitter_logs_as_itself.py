"""The annotator's lineage record is its own, and only a local-mode save writes one.

The record is logged under the emitting module's logger, so its level and its attribution are the annotator's,
and it reaches the operator through the logging system (the request-id filter, the level gate, the OTLP export)
and never past it. A save through a live catalog writes none: the catalog announces every write it commits, so a
second record would count the run twice.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from annotator.annotations import lineage
from annotator.core.config import AnnotatorSettings, LineageSink


_LOGGER = "annotator.annotations.lineage"


class _FakeDataset:
    """Enough of a `lance.LanceDataset` for the event builder: a schema, a version, a row count."""

    version = 7
    schema = pa.schema([pa.field("id", pa.string()), pa.field("shapes", pa.string())])

    def count_rows(self) -> int:
        return 3


def _emit(sink: str) -> dict[str, Any] | None:
    return lineage.emit_save(
        # cast: the builder reads only `schema`, `version` and `count_rows()`, and constructing a real
        # `lance.LanceDataset` here would make a logger-name test depend on writing a Lance table.
        ds=cast("lance.LanceDataset", _FakeDataset()),
        table_uri="s3://bucket/annotations.lance",
        table_name="annotations",
        unit_key="unit-1",
        sink=sink,
    )


def test_the_record_carries_the_module_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        event = _emit(LineageSink.log)
    assert event is not None
    records = [r for r in caplog.records if r.message.startswith("openlineage ")]
    assert len(records) == 1
    assert records[0].name == _LOGGER, "the record claims another module produced it"


def test_nothing_writes_past_the_logging_system(capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger(_LOGGER).setLevel(logging.CRITICAL)
    try:
        _emit(LineageSink.log)
    finally:
        logging.getLogger(_LOGGER).setLevel(logging.NOTSET)
    assert capsys.readouterr().out == "", "a record suppressed by the level gate still reached stdout"


def test_the_none_sink_still_emits_nothing() -> None:
    assert _emit(LineageSink.none) is None


def test_a_save_through_the_catalogs_rest_door_leaves_its_lineage_to_the_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert AnnotatorSettings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)}).effective_lineage_sink is LineageSink.log
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    assert AnnotatorSettings.model_validate({"MEDIA_CATALOG_URI": "http://catalog:2333"}).effective_lineage_sink is LineageSink.none
