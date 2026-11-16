"""The annotator, driven from its own release, works the lakehouse through its public doors only (XC-167, ADR 0171).

The annotator is a client of the lakehouse like Label Studio would be: it keeps its projects, tasks and roles in its own
state, reads a governed media table through the catalog as the person, and writes annotations and publishes as the
workbench's own confidential client, which a project admin granted writer on the publish target through the catalog's
grant door. Which principal that client is, the catalog says (`GET /management/v1/me` on its client-credentials token).
One drive, against the deployed estate:

* a scratch tenant's admin seeds a governed media dataset (a chunks table stamped with its descriptor), grants the
  annotating person reader on it, creates the publish target under the run rule (`rask.requires_run`), and grants the
  workbench's client writer on the target and reader on the inputs;
* the admin creates a project over the dataset; the annotating person, a member of no tenant, is refused it until the
  admin grants them a role on the project, which is the annotator's own decision;
* the admin sends the item and assigns it to the person, who saves an annotation on its unit: the annotator writes the
  project's own annotations table in the workbench's namespace (`<namespace>$<project>__<dataset digest>`) as the
  workbench's client, naming the person as the commit's originator;
* the person drafts and submits; the admin accepts, freezes the project and publishes it, and the workbench's client
  commits the published table, naming the selected version of the row table as its input;
* every table the drive wrote is read back through the catalog with pylance's stock client.

Run against a deployed rask lakehouse with the annotator installed beside it, naming the endpoints in the
`LANCE_E2E_*` variables `lakehouse_doors` reads and `LANCE_E2E_ANNOTATOR_URL`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from collections.abc import Iterator
from typing import Any

import lakehouse_doors
import lance
import pyarrow as pa
import pytest
import requests
from lakehouse_doors import CATALOG, Identity, Tenant, ok, rask_properties
from pydantic import BaseModel

from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY


ANNOTATOR = os.environ.get("LANCE_E2E_ANNOTATOR_URL", "").rstrip("/")
#: The annotator's `ANNOTATOR_ANNOTATIONS_NAMESPACE`, where each project's annotation tables live.
ANNOTATIONS_NAMESPACE = os.environ.get("LANCE_E2E_ANNOTATIONS_NAMESPACE", "workbench$annotations")
#: The annotator's `ANNOTATOR_PUBLISH_NAMESPACE`: the namespace of a project's warehouse a publish naming none lands in.
PUBLISH_NAMESPACE = os.environ.get("LANCE_E2E_PUBLISH_NAMESPACE", "published")
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "180"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.annotator,
    pytest.mark.skipif(
        not (
            ANNOTATOR
            and CATALOG
            and lakehouse_doors.DEX
            and lakehouse_doors.DEX_CLIENT
            and lakehouse_doors.DEX_AUDIENCE
            and lakehouse_doors.WORKBENCH_CLIENT
            and lakehouse_doors.WORKBENCH_CLIENT_SECRET_FILE
            and lakehouse_doors.LAKEHOUSE_AUDIENCE
        ),
        reason="LANCE_E2E_ANNOTATOR_URL, LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX, LANCE_E2E_OIDC_CLIENT_ID, LANCE_E2E_OIDC_AUDIENCE, "
        "LANCE_E2E_WORKBENCH_CLIENT_ID, LANCE_E2E_WORKBENCH_CLIENT_SECRET_FILE and LANCE_E2E_LAKEHOUSE_AUDIENCE",
    ),
]

#: The media dataset's declared row identity, and the one searchable table that carries it.
DESCRIPTOR = {"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}, "searches": [{"name": "default", "row_table": "chunks"}]}
IDENTITY = [("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64())]
UNIT = ("d1", 0, 0)
LABEL = "seal"


class Drive(BaseModel):
    """What the drive did and what came of it, read once it ended."""

    project_id: str
    #: The annotating person's answer for the project before and after the admin granted them a role on it.
    refused: int
    admitted: int
    #: The principal the catalog names the workbench's client by.
    workbench: str
    #: The project's annotations table, read back as the workbench's client, and who the catalog announced each write to
    #: it as.
    annotations: list[dict[str, Any]]
    annotation_authors: list[str]
    #: The version of the corpus's row table the person selected the item from.
    selected: int
    #: The published table, read back as the admin, and what its commit carries.
    published_table: str
    published: list[dict[str, Any]]
    published_commit: dict[str, Any]


def _stream(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create_table(admin: Identity, table_id: str, table: pa.Table) -> None:
    answer = admin.http.post(
        f"{CATALOG}/v1/table/{table_id}/create", data=_stream(table), headers={"content-type": "application/vnd.apache.arrow.stream"}, timeout=60
    )
    ok(answer)


def _seed_corpus(tenant: Tenant) -> None:
    """The governed media dataset: chunks stamped with the descriptor."""
    chunks = pa.Table.from_pylist(
        [{"doc_id": UNIT[0], "speech_id": UNIT[1], "chunk_id": UNIT[2], "text": "a line"}],
        schema=pa.schema([*IDENTITY, ("text", pa.string())], metadata={DESCRIPTOR_METADATA_KEY: json.dumps(DESCRIPTOR).encode()}),
    )
    table_id = f"{tenant.media}$chunks"
    _create_table(tenant.admin, table_id, chunks)
    tenant.tables.append(table_id)


def _annotations_table(project_id: str, dataset: str) -> str:
    """The project's annotations table on ``dataset``, named as `annotator.annotations.store.table_name` names it."""
    return f"{ANNOTATIONS_NAMESPACE}${project_id}__{hashlib.sha256(dataset.encode()).hexdigest()[:16]}"


def _grant(admin: Identity, namespace: str, user: str, relation: str) -> None:
    ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{namespace}/access/grant", json={"user": user, "relation": relation}))


def _annotator(identity: Identity) -> requests.Session:
    session = requests.Session()
    session.headers["authorization"] = f"Bearer {identity.bearer}"
    return session


def _answer(response: requests.Response, expected: int = 200) -> dict[str, Any]:
    assert response.status_code == expected, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:500]}"
    return response.json()


def _commit(identity: Identity, table_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A table's rows and the `rask.*` properties of its newest commit, read through the catalog by ``identity``."""
    dataset = lance.dataset(namespace_client=identity.namespace(), table_id=table_id.split("$"))
    transaction = dataset.read_transaction(dataset.version)
    assert transaction is not None, f"{table_id} version {dataset.version} has no transaction to read"
    return dataset.to_table().to_pylist(), rask_properties(transaction.transaction_properties or {})


def _published(admin: requests.Session, project_id: str) -> dict[str, Any]:
    """The project once its publish ended, read until it is published or failed."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        project = _answer(admin.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30))["project"]
        if project["state"] in {"published", "publish_failed"}:
            return project
        assert time.monotonic() < deadline, f"project {project_id} is still {project['state']} after {SETTLE_SECONDS:.0f} s"
        time.sleep(3)


def _authors(reader: Identity, table_id: str, *, since: int, after: int) -> list[str]:
    """Who the catalog announced each write to ``table_id`` past version ``after`` as, read off lineage's feed from row
    ``since``. By version, since the feed lags the commit: the table's own create can arrive after ``since`` was read.

    A write that names no run carries no `rask.*` transaction property, so its commit announcement is where the catalog
    records who made it: the verified caller, as the author facet.
    """

    def past(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if int(lakehouse_doors.lance_facet(row).get("version") or 0) > after]

    rows, _ = lakehouse_doors.follow(reader, since=since, table=table_id, until=lambda rows: bool(past(rows)))
    # The author rides the run's facets on a RunEvent and the dataset's on a DatasetEvent.
    carriers = [row["event"].get("run") if "run" in row["event"] else row["event"].get("dataset") for row in past(rows)]
    return [str(((carrier or {}).get("facets") or {}).get("rask_author", {}).get("sub")) for carrier in carriers]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from lakehouse_doors.provisioned("annot", grants=("reader",))


@pytest.fixture(scope="module")
def drive(tenant: Tenant) -> Iterator[Drive]:
    """The drive, and the project archived after it: its state is the annotator's, which the tenant's teardown never reaches."""
    admin, person = tenant.admin, tenant.job
    workbench = lakehouse_doors.Identity.workbench()
    target = f"{tenant.project}${PUBLISH_NAMESPACE}"
    ok(admin.http.post(f"{CATALOG}/v1/namespace/{target}/create", json={"properties": {"rask.requires_run": "true"}}))
    _seed_corpus(tenant)
    # A project admin hands the workbench's client what its publish needs, and nothing more.
    _grant(admin, target, workbench.principal, "writer")
    _grant(admin, tenant.media, workbench.principal, "reader")

    as_admin, as_person = _annotator(admin), _annotator(person)
    project_id = str(_answer(as_admin.post(f"{ANNOTATOR}/projects", json={"tenant": tenant.project, "slug": "seals"}, timeout=30), 201)["project_id"])
    refused = as_person.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30).status_code
    _answer(as_admin.put(f"{ANNOTATOR}/projects/{project_id}/members", json={"user": person.principal, "relation": "annotator"}, timeout=30))
    admitted = as_person.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30).status_code

    # The item carries the version of the corpus's row table the person selected from, as the app captures it, which
    # the publish names as its input.
    selected = lance.dataset(namespace_client=person.namespace(), table_id=[tenant.media, "chunks"]).version
    key = "/".join(map(str, UNIT))
    source = {"kind": "chunks", "keys": [key], "where": tenant.media, "dataset_version": selected}
    [task_id] = _answer(
        as_admin.post(f"{ANNOTATOR}/projects/{project_id}/items", json={"items": [{"source": source, "media": {"kind": "image"}}]}, timeout=60), 201
    )["task_ids"]
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "open"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "assign", "assignee": person.principal}, timeout=30))

    # The person annotates the unit their task holds: the annotator writes the project's own table as the workbench's
    # client, after its own check on the project and the task.
    unit = f"{ANNOTATOR}/api/annotations/{key}"
    scope = {"project": project_id, "task": task_id, "dataset": tenant.media}
    annotations_table = _annotations_table(project_id, tenant.media)
    reader = Identity(admin.bearer)
    feed = lakehouse_doors.feed_start(reader)
    loaded = as_person.get(unit, params=scope, timeout=60)
    assert loaded.status_code == 200, f"GET {unit} -> {loaded.status_code}: {loaded.text[:300]}"
    save = {"inserts": [{"id": "a1", "shape_type": "rectangle", "x": 10, "y": 10, "width": 20, "height": 20, "label": LABEL}]}
    save["base_version"] = int(loaded.headers["X-Annotations-Version"])
    _answer(as_person.post(unit, params=scope, json=save, timeout=60))
    tenant.tables.append(annotations_table)

    shape = {"shape_type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4, "label": LABEL}
    _answer(as_person.put(f"{ANNOTATOR}/tasks/{task_id}/draft", json={"shapes": [shape]}, timeout=30))
    _answer(as_person.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "submit"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "accept"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "freeze"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "publish"}, timeout=30))
    project = _published(as_admin, project_id)
    assert project["state"] == "published", f"the publish failed: {project.get('publish_error') or project}"
    published_table = str(project["published"]["table_id"])
    tenant.tables.append(published_table)

    annotations, _ = _commit(workbench, annotations_table)
    published, published_commit = _commit(admin, published_table)
    yield Drive(
        project_id=project_id,
        refused=refused,
        admitted=admitted,
        workbench=workbench.principal,
        annotations=annotations,
        annotation_authors=_authors(reader, annotations_table, since=feed, after=0),
        selected=selected,
        published_table=published_table,
        published=published,
        published_commit=published_commit,
    )
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "archive"}, timeout=30))


def test_the_annotator_works_the_lakehouse_through_its_public_doors(drive: Drive, tenant: Tenant) -> None:
    """The closes-when of XC-167, read off the one drive.

    The annotator decides its own project roles: a person of no tenant is refused the project until the admin grants them
    a role on it. It holds no store credential: the annotation and the publish are written through the catalog as the
    workbench's client, so the catalog announces both as that client, and the published rows name who annotated and who
    reviewed from the task's own record. The publish lands in the project's own warehouse, under the run rule, naming the
    selected version of the row table as its input.
    """
    assert (drive.refused, drive.admitted) == (403, 200)
    assert [(row["id"], row["label"]) for row in drive.annotations] == [("a1", LABEL)]
    assert drive.annotation_authors and set(drive.annotation_authors) == {drive.workbench}, drive.annotation_authors
    rows = [(row["project_id"], row["label"], row["annotated_by"], row["reviewed_by"]) for row in drive.published]
    assert rows == [(drive.project_id, LABEL, tenant.job.principal, tenant.admin.principal)]
    assert drive.published_table.startswith(f"{tenant.project}${PUBLISH_NAMESPACE}$")
    assert drive.published_commit.get("rask.author") == drive.workbench
    assert drive.published_commit.get("rask.inputs") == [{"table": [tenant.media, "chunks"], "version": drive.selected}]
