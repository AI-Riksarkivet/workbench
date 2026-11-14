"""A cached search result is served only to a caller whose own read of the searched tables the catalog admits.

The result cache is per app, shared by every caller, and keyed on the versions of the tables a search reads. Those
versions are read by opening each table as the caller, which is also what entitles the caller to the entry: a caller the
catalog refuses gets the refusal, never another caller's cached rows, and a catalog that cannot be asked leaves the
request uncached rather than keyed on versions nobody read. Driven against a REST catalog on a real socket that answers
per bearer (``tests.vending_catalog``), over a real local table.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from client_kit.lancekit.registry import DatasetRegistry
from client_kit.media.config import MediaSettings
from search.services.result_cache import run_cached
from search.services.spec import SearchSpec
from service_kit.exceptions import ForbiddenError
from tests.media_catalog import write_table
from tests.vending_catalog import VendingCatalog


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}
_ROWS = pa.table({"doc_id": ["a"]}, schema=pa.schema([("doc_id", pa.string())], metadata={b"lance_media.descriptor": json.dumps(_DESCRIPTOR).encode()}))


def test_a_refused_caller_is_not_served_what_another_caller_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A vend that has already expired, so no open is reused and every request asks the catalog again.
    expired = {"expires_at_millis": str(int(time.time() * 1000))}
    catalog = VendingCatalog(write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS).uri, {"caller-a": expired, "caller-b": expired})
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    registry = DatasetRegistry(MediaSettings.model_validate({"MEDIA_CATALOG_URI": catalog.url}))
    as_a, as_b = registry.get("wh$ns", "caller-a"), registry.get("wh$ns", "caller-b")
    cache: dict[Any, Any] = {}
    spec = SearchSpec.model_validate({"q": "cat"})
    catalog.answers["caller-a"] = "fail"
    assert run_cached(cache, 16, 0, as_a, spec, None, None, lambda: [{"doc_id": "a", "seen_by": "caller-a"}])
    catalog.answers["caller-b"] = "refuse"

    def _must_not_run() -> list[dict[str, Any]]:
        raise AssertionError("the refused caller's search ran")

    with pytest.raises(ForbiddenError):
        run_cached(cache, 16, 0, as_b, spec, None, None, _must_not_run)

    catalog.close()
    assert cache == {}
