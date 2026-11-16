"""The workbench's annotation tables: one per (project, dataset), in the lakehouse namespace the workbench owns.

The workbench owns its annotation tables in the lakehouse and holds the write grant there; a person needs only a read
grant on the datasets they annotate. So an annotation table is never in a dataset's own namespace, where any project
annotating that dataset would share it: it lives in ``ANNOTATOR_ANNOTATIONS_NAMESPACE`` and is named by the project and
a digest of the dataset id, so every row belongs to exactly one project and a write made for one project cannot touch
another's rows, whatever ids it names.

Every read and write here is the workbench's own identity (its client-credentials token), after the annotator's own
check on the project (``annotator.api.security.project_gate``); a write names the person in its commit
(``x-lance-originator``). A table is created on a project's first save, with the dataset's identity columns and the
contract's (``EMPTY_SCHEMA``).
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable

import lance
import lance_namespace
import pyarrow as pa
from lance_namespace import CreateNamespaceRequest, NamespaceAlreadyExistsError
from pydantic import BaseModel, ConfigDict

from annotator.annotations.schema import EMPTY_SCHEMA
from client_kit.lancekit.absence import reads_as_absent
from client_kit.lancekit.catalog import NamespaceCatalogWriteTransport, as_workbench, bounded, lakehouse_writer, translate_catalog_errors
from client_kit.lancekit.descriptor import Declared
from client_kit.media.config import MediaSettings
from client_kit.sources import RaskSource
from service_kit.exceptions import ConflictError, NotFoundError


def table_name(project: str, dataset_id: str) -> str:
    """The table of ``project``'s annotations on ``dataset_id``: the project id and a digest of the dataset id, since a
    dataset id carries the catalog's delimiter."""
    return f"{project}__{hashlib.sha256(dataset_id.encode()).hexdigest()[:16]}"


def table_schema(declared: Declared) -> pa.Schema:
    """A new table's columns: the dataset's identity (its doc key a string, every other key an int64) and the contract."""
    identity = declared.identity
    keys = [pa.field(identity.doc_key, pa.string())] + [pa.field(f, pa.int64()) for f in identity.key_fields if f != identity.doc_key]
    return pa.schema([*keys, *EMPTY_SCHEMA])


class ProjectAnnotations(BaseModel):
    """One project's annotations on one dataset, reached as the workbench's own identity."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    lakehouse: RaskSource
    namespace: list[str]
    name: str
    token: Callable[[], str] | None

    @classmethod
    def of(cls, settings: MediaSettings, namespace: str, project: str, dataset_id: str, token: Callable[[], str] | None) -> ProjectAnnotations:
        """The table in the deployment's lakehouse, the one source annotations are written to.

        Raises:
            ConflictError: The deployment reads no lakehouse, so nothing it reads is annotated.
        """
        lakehouse = settings.sources.lakehouse
        if lakehouse is None:
            raise ConflictError("this deployment reads no rask lakehouse, and only a lakehouse's datasets are annotated")
        return cls(lakehouse=lakehouse, namespace=namespace.split(lakehouse.delimiter), name=table_name(project, dataset_id), token=token)

    def _client(self) -> lance_namespace.LanceNamespace:
        """The annotation tables are read as they are written: as the workbench's own identity (`as_workbench`)."""
        return as_workbench(self.lakehouse, self.token)

    @property
    def table_id(self) -> list[str]:
        return [*self.namespace, self.name]

    def open(self) -> lance.LanceDataset | None:
        """The table at its latest version, or ``None`` before the project's first save on the dataset."""
        client = self._client()
        try:
            with translate_catalog_errors():
                return bounded(lambda: lance.dataset(namespace_client=client, table_id=self.table_id))
        except NotFoundError:
            return None
        except (ValueError, OSError) as exc:
            if reads_as_absent(exc):
                return None
            raise

    def writer(self, person: str) -> NamespaceCatalogWriteTransport:
        """The catalog's write doors for the table, naming ``person`` as the originator of each commit."""
        return lakehouse_writer(self.lakehouse, self.table_id, token=self.token, originator=person)

    def open_or_create(self, declared: Declared, person: str) -> lance.LanceDataset:
        """The table, created empty with the dataset's identity columns when the project has none yet."""
        existing = self.open()
        if existing is not None:
            return existing
        client = self._client()
        for depth in range(1, len(self.namespace) + 1):
            with translate_catalog_errors(), contextlib.suppress(NamespaceAlreadyExistsError):
                bounded(lambda d=depth: client.create_namespace(CreateNamespaceRequest(id=self.namespace[:d])))
        # Created empty through the namespace (DeclareTable and the credential it vends), the stock client's create: a
        # CreateTable carries rows, and a new project's table has none. File format 2.2 and stable row ids, as every
        # governed table the lakehouse creates.
        writer_client = self.writer(person).namespace
        bounded(
            lambda: lance.write_dataset(
                table_schema(declared).empty_table(),
                namespace_client=writer_client,
                table_id=self.table_id,
                mode="create",
                data_storage_version="2.2",
                enable_stable_row_ids=True,
            )
        )
        created = self.open()
        if created is None:
            raise NotFoundError(f"the annotations table {self.name} was created and cannot be opened")
        return created


__all__ = ["ProjectAnnotations", "table_name", "table_schema"]
