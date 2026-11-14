"""A table the catalog does not hold is the estate's one absence, a 404.

The registry opens a table through the namespace, which answers a missing table with lance-namespace's typed
``TableNotFoundError``; the registry renders it as the platform's ``NotFoundError``.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from client_kit.lancekit.registry import DatasetRegistry
from client_kit.media.config import MediaSettings
from service_kit.exceptions import NotFoundError
from service_kit.governed.deps import ANONYMOUS_CALLER
from tests.media_catalog import write_table


def test_a_missing_table_of_a_dataset_is_not_found(tmp_path: Path) -> None:
    write_table(tmp_path, ["wh", "ns", "chunks"], pa.table({"doc_id": ["a"]}), descriptor={"identity": {"key_fields": ["doc_id"]}})
    handle = DatasetRegistry(MediaSettings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)})).get("wh$ns", ANONYMOUS_CALLER)

    with pytest.raises(NotFoundError, match="wh\\$ns\\$nope"):
        handle.dataset("nope")
