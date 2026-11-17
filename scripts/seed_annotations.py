"""Seed one project's annotations table on one dataset for the annotator wire, through a lance-namespace catalog.

Writes the project's table in the workbench's annotations namespace (``annotator.annotations.store``: one table per
project and dataset) with a few sample shapes on a real frame, through the catalog's CreateTable door
(``mode=overwrite``), so a re-seed converges the fixture. The contract columns come STRAIGHT from the backend's
``EMPTY_SCHEMA`` (the single source of truth — ``services/annotator/annotations/schema.py``); this script only prepends
the demo descriptor's identity columns. A test asserts the seeded table matches the composition, so drift fails loudly.

The catalog is the one the media plane reads: ``MEDIA_CATALOG_DIR`` (lance-namespace's directory catalog, for a local
stack) or ``MEDIA_CATALOG_URI`` (the catalog's REST door, as ``MEDIA_CATALOG_TOKEN``'s bearer), where the table's
maintenance policy is set too.

    MEDIA_CATALOG_DIR=<root> uv run python scripts/seed_annotations.py <annotations warehouse>$<namespace> <project> <dataset> <task> [doc_id]

The rows are the named task's work (`task_id`): a unit's rows are read and written per task.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import UTC, datetime

import httpx
import lance
import lance_namespace
import pyarrow as pa
from lance_namespace import CreateNamespaceRequest, CreateTableRequest, LanceNamespace, NamespaceAlreadyExistsError

from annotator.annotations.schema import EMPTY_SCHEMA
from annotator.annotations.store import table_name


SCHEMA = pa.schema(
    [
        # identity (the demo descriptor's key fields) — the ONLY columns this script
        # defines; everything else is the backend contract, imported verbatim.
        ("doc_id", pa.string()),
        ("speech_id", pa.int64()),
        ("chunk_id", pa.int64()),
        ("frame_idx", pa.int64()),
        *EMPTY_SCHEMA,
    ]
)


def sample_table(doc_id: str, task: str) -> pa.Table:
    """3 sample rows of ``task``'s: 2 model predictions (varying confidence/uncertainty) + 1 human-accepted."""
    now = datetime.now(UTC)
    rows = {
        "doc_id": [doc_id] * 3,
        "speech_id": [0] * 3,
        "chunk_id": [19] * 3,
        "frame_idx": [0] * 3,
        "id": ["a1", "a2", "a3"],
        "task_id": [task] * 3,
        "shape_type": ["rectangle", "rectangle", "polygon"],
        "x": [40.0, 210.0, 0.0],
        "y": [40.0, 150.0, 0.0],
        "width": [160.0, 120.0, 0.0],
        "height": [90.0, 80.0, 0.0],
        "rotation": [0.0, 0.0, 0.0],
        "polygon": [[], [], [120.0, 240.0, 260.0, 250.0, 190.0, 320.0]],
        "t_start": [0.0, 0.0, 0.0],  # image annotations have no time axis
        "t_end": [0.0, 0.0, 0.0],
        "text": ["regeringen", "principmodellen", "region"],
        # The TEXTUAL facet. Empty here on purpose: none of these three rows is a span, and a
        # non-span row must carry `-1` rather than 0 — 0 is a meaningful offset (the first
        # character), so zeros would seed three zero-length spans pointing at nothing.
        "parent_id": ["", "", ""],
        "char_start": [-1, -1, -1],
        "char_end": [-1, -1, -1],
        "label": ["text-line", "text-line", "figure"],
        "status": ["prediction", "prediction", "accepted"],
        "source": ["model:htr-trocr@v1", "model:htr-trocr@v1", "manual"],
        "reviewer": ["", "", "gabriel"],
        "confidence": [0.88, 0.61, 1.0],  # a2 is low-confidence → top of the review queue
        "uncertainty": [0.18, 0.72, 0.0],
        "model_version": ["htr-trocr@v1", "htr-trocr@v1", ""],
        "group": ["lines", "lines", "figures"],
        "group_id": ["", "", ""],
        "reading_order": [0, 1, -1],
        "difficult": [False, True, False],
        "links": ["[]", "[]", "[]"],
        "mask": ["", "", ""],
        "metadata": ["{}", "{}", "{}"],
        "created_at": [now] * 3,
        "updated_at": [now] * 3,
    }
    return pa.table(rows, schema=SCHEMA)


def seed(client: LanceNamespace, namespace: list[str], table: str, doc_id: str, task: str) -> list[str]:
    """OVERWRITE ``table`` in ``namespace`` with the sample rows, creating the namespace's levels the catalog lacks; returns
    the table's id."""
    for depth in range(1, len(namespace) + 1):
        with contextlib.suppress(NamespaceAlreadyExistsError):
            client.create_namespace(CreateNamespaceRequest(id=namespace[:depth]))
    rows = sample_table(doc_id, task)
    body = io.BytesIO()
    with pa.ipc.new_stream(body, rows.schema) as writer:
        writer.write_table(rows)
    table_id = [*namespace, table]
    client.create_table(CreateTableRequest(id=table_id, mode="overwrite"), body.getvalue())
    return table_id


def set_maintenance_policy(catalog_uri: str, table_id: list[str], token: str | None) -> None:
    """Re-assert the table's maintenance policy at the catalog's management door, so a re-seed converges the whole
    state: the policy lives outside the table (`docs/rask/catalog-openapi.json`, ``/management/v1/table/{id}/policy/set``).
    A directory catalog has no such door and keeps no policy."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.post(
        f"{catalog_uri.rstrip('/')}/management/v1/table/{'$'.join(table_id)}/policy/set",
        json={"retention_days": 30, "retain_versions": 100, "compact_enabled": True},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()


def _client() -> LanceNamespace:
    """The catalog the media plane reads, from the same settings it reads them from."""
    root = os.environ.get("MEDIA_CATALOG_DIR")
    uri = os.environ.get("MEDIA_CATALOG_URI")
    if bool(root) == bool(uri):
        raise SystemExit("set exactly one of MEDIA_CATALOG_DIR and MEDIA_CATALOG_URI")
    if root:
        return lance_namespace.connect("dir", {"root": root})
    properties = {"uri": str(uri)}
    if token := os.environ.get("MEDIA_CATALOG_TOKEN"):
        properties["header.Authorization"] = f"Bearer {token}"
    return lance_namespace.connect("rest", properties)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit("usage: seed_annotations.py <annotations warehouse>$<namespace> <project> <dataset> <task> [doc_id]")
    namespace = sys.argv[1].split("$")
    if len(namespace) < 2 or not all(namespace):
        raise SystemExit("name the annotations namespace as <warehouse>$<namespace>: a name's first segment is its warehouse")
    doc = sys.argv[5] if len(sys.argv) > 5 else "fe00cd746463ad2c"
    client = _client()
    table_id = seed(client, namespace, table_name(sys.argv[2], sys.argv[3]), doc, sys.argv[4])
    if uri := os.environ.get("MEDIA_CATALOG_URI"):
        set_maintenance_policy(uri, table_id, os.environ.get("MEDIA_CATALOG_TOKEN"))
    ds = lance.dataset(namespace_client=client, table_id=table_id)
    print(f"seeded {ds.count_rows()} annotations → {'$'.join(table_id)} (v{ds.version})")
    print("columns:", [f.name for f in ds.schema])
