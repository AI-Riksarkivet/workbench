"""The annotator's corpus: media datasets read as the person, through lance-namespace.

A media dataset is a namespace of a declared source (`workbench.sources`): a lakehouse's are always annotated, any
other source's only where git marks it annotatable (`workbench.sources.annotatable`). A lakehouse's is opened through
the namespace with the person's bearer (the media registry, :mod:`workbench.lancekit.registry`), so pylance asks
`DescribeTable` for the credential the catalog vends that person and reads with it, and a table the person may not read
is refused at the catalog. Any other source is read with no token through its confined namespace, after the workbench's
own check that the person reads it (`workbench.media.source_gate`). The annotator holds no store credential.

A dataset's declared roles (row identity, the document binding) come from the `lance_media.descriptor` stamp on one of
its tables, checked against the tables before a route uses them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import lance
import lance_namespace
from pydantic import BaseModel, ConfigDict

from workbench.exceptions import ConflictError, ForbiddenError, NotFoundError, ServiceUnavailableError
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.absence import reads_as_absent
from workbench.lancekit.catalog import bounded
from workbench.lancekit.descriptor import Declared
from workbench.lancekit.registry import DatasetHandle
from workbench.media.state import dataset_handle
from workbench.sources import RaskSource, annotatable, connect_as


if TYPE_CHECKING:
    from workbench.media.state import AppState


logger = logging.getLogger(__name__)


class Corpus(BaseModel):
    """What a route needs of one media dataset: its id, its declared roles, and the person's handle on it, which every
    read and write of its tables goes through."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    declared: Declared
    handle: DatasetHandle


def resolve(state: AppState, dataset_id: str | None, caller: LakehouseCaller) -> Corpus:
    """One media dataset, as the caller. Blocking (catalog calls, a manifest read per table), so an async route runs it in
    the threadpool."""
    handle = dataset_handle(state, dataset_id, caller)
    if handle.descriptor.read_only is not None:
        # A source git does not mark annotatable (`workbench.sources.annotatable`) is never sent to a project, labelled
        # or published, so no write can follow from reading it.
        raise ConflictError(f"dataset {handle.id!r} cannot be annotated: {handle.descriptor.read_only}")
    return Corpus(id=handle.id, declared=handle.declared, handle=handle)


def _is_table(client: lance_namespace.LanceNamespace, identifier: list[str]) -> bool:
    """Whether the catalog holds ``identifier`` as a table; a nested namespace of the same depth answers no.

    Asked with DescribeTable, which the spec answers TableNotFound for anything but a table, asking no credential (a
    classification reads no bytes) and bounded like every catalog call (`workbench.lancekit.catalog.bounded`, whose
    timeout is a ServiceUnavailableError, which `input_tables` reads as a check that did not run). TableExists and
    NamespaceExists are not: measured 2026-11-14 on lance-namespace's ``dir`` catalog (pylance 13.0.0), each answers
    yes for a nested namespace's id and for a table's id alike.
    """
    try:
        bounded(lambda: client.describe_table(lance_namespace.DescribeTableRequest(id=identifier, vend_credentials=False)))
    except lance_namespace.TableNotFoundError:
        return False
    return True


def not_annotatable(state: AppState, names: list[str]) -> list[str]:
    """Why each of ``names`` whose source is not marked annotatable cannot be annotated; a name naming no declared source
    is left to the catalog check, which answers it as unseen."""
    reasons: list[str] = []
    for name in names:
        try:
            source = state.settings.sources.locate(name).source
        except ValueError:
            continue
        if (reason := annotatable(source)) is not None:
            reasons.append(f"{name}: {reason}")
    return reasons


def input_tables(state: AppState, names: list[str], caller: LakehouseCaller) -> dict[str, str | None] | None:
    """For each of ``names`` its source shows the caller, the table an item's captured version is a version of.

    A name is qualified by its source (`workbench.sources`) and, in a lakehouse, written with its warehouse first. A name
    its source holds as a table (in a lakehouse, one of three or more segments) is its own table; any other is a dataset
    (a namespace, possibly nested, or a source's root), whose items are rows of its default search's row table: the table
    whose version a sender captures (the zone's `rowTableVersion`). Every item thus records its table and version, from
    any source marked annotatable; a publish names a lakehouse's as its lineage inputs, which a target under the run rule
    requires, and writes every item's into its rows (`annotator.projects.publish`). None for a dataset that declares no
    search. A name missing from the answer is one the caller cannot open on the canvas: absent, refused, of a source not
    marked annotatable, or a dataset with no valid descriptor. None when a catalog could not be asked.
    """
    sources = state.settings.sources
    found: dict[str, str | None] = {}
    for name in names:
        try:
            located = sources.locate(name)
            client = connect_as(located.source, caller)
            if len(located.identifier) >= (3 if isinstance(located.source, RaskSource) else 1) and _is_table(client, located.identifier):
                found[name] = name
            else:
                corpus = resolve(state, name, caller)
                search = corpus.declared.search
                found[name] = corpus.handle.qualified_table(search.row_table) if search else None
        except (
            ValueError,
            lance_namespace.TableNotFoundError,
            lance_namespace.NamespaceNotFoundError,
            lance_namespace.PermissionDeniedError,
            NotFoundError,
            ForbiddenError,
            ConflictError,
        ):
            continue
        except (lance_namespace.LanceNamespaceError, ServiceUnavailableError):
            logger.warning("send could not ask the catalog about %s; not refusing on a check that did not run", name)
            return None
    return found


def unheld_versions(state: AppState, captured: set[tuple[str, int]], caller: LakehouseCaller) -> list[str] | None:
    """Which of ``captured`` (a table and the version a sender says they saw of it) the catalog does not hold, as the
    caller; None when the catalog could not be asked.

    A sender names the version of the row table they were looking at, and a publish names it as its run's input, so the
    send accepts it only when the table has that version: opened at it through the namespace, as the caller, with Lance's
    own checkout (`lance.dataset(..., version=)`), which reads that version's manifest and finds none past the table's
    latest or one cleanup removed. Measured 2026-11-16 on the ``dir`` catalog (pylance 13.0.0): DescribeTable ignores
    the request's ``version`` there, while the checkout refuses a version the table does not have.
    """
    unheld: list[str] = []
    for table, version in sorted(captured):
        try:
            located = state.settings.sources.table_named(table)
        except ValueError:
            unheld.append(f"version {version} of {table}")
            continue
        client = connect_as(located.source, caller)
        try:
            bounded(lambda c=client, i=located.identifier, v=version: lance.dataset(namespace_client=c, table_id=i, version=v))
        except (ValueError, OSError) as exc:
            if not reads_as_absent(exc):
                logger.warning("send could not read version %s of %s; not accepting a version it did not check", version, table)
                return None
            unheld.append(f"version {version} of {table}")
        except (lance_namespace.LanceNamespaceError, ServiceUnavailableError):
            logger.warning("send could not ask the catalog about %s; not accepting a version it did not check", table)
            return None
    return unheld
