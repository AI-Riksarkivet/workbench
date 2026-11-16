"""The media plane's datasets on lance-namespace's stock directory catalog, for the offline suites.

The media services read datasets as catalog namespaces (``warehouse$namespace``) through a namespace client, and the
same code runs on the ``dir`` catalog as on the catalog's REST door. So a suite builds its datasets here, under
``tmp_path``, through the same doors, and declares the root as the deployment's lakehouse source (:func:`lakehouse_dir`,
named :data:`LAKE`), or a REST catalog as one (:func:`lakehouse_rest`). A dataset's id then carries the source:
``lake:acme$corpus`` (:func:`lake`).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import lance
import lance_namespace
import pyarrow as pa
from lance_namespace import CreateNamespaceRequest, LanceNamespace, NamespaceAlreadyExistsError


#: The lakehouse source's name in every suite.
LAKE = "lake"


def lake(identifier: str) -> str:
    """A dataset's or table's qualified name in the suites' lakehouse source."""
    return f"{LAKE}~{identifier}"


def lakehouse_dir(root: Path | str) -> dict[str, object]:
    """The settings declaring the directory catalog at ``root`` as the deployment's lakehouse (`client_kit.sources`)."""
    return {"WORKBENCH_SOURCES": [{"kind": "rask", "name": LAKE, "dir": str(root), "identity": "none"}]}


def lakehouse_rest(uri: str) -> dict[str, object]:
    """The settings declaring the REST catalog at ``uri`` as the deployment's lakehouse, read as the person."""
    return {"WORKBENCH_SOURCES": [{"kind": "rask", "name": LAKE, "uri": uri}]}


def namespace_client(root: Path) -> LanceNamespace:
    """The directory catalog at ``root``."""
    return lance_namespace.connect("dir", {"root": str(root)})


def write_table(root: Path, table_id: Sequence[str], data: pa.Table, *, descriptor: Mapping[str, object] | None = None) -> lance.LanceDataset:
    """Create ``table_id`` (its namespace's levels too, when the catalog lacks them) holding ``data``.

    Written as a media table is: file format 2.2 (blob v2 needs it) with stable row ids (the media routes take blobs by
    them). ``descriptor`` is stamped on the table's schema metadata, where the registry reads a dataset's declared roles.
    """
    client = namespace_client(root)
    for depth in range(1, len(table_id)):
        with contextlib.suppress(NamespaceAlreadyExistsError):
            client.create_namespace(CreateNamespaceRequest(id=list(table_id[:depth])))
    ds = lance.write_dataset(data, namespace_client=client, table_id=list(table_id), mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    if descriptor is not None:
        ds.update_schema_metadata({"lance_media.descriptor": json.dumps(descriptor)})
        ds = lance.dataset(namespace_client=client, table_id=list(table_id))
    return ds
