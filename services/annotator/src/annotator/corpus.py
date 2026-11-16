"""The annotator's corpus: media datasets read as the person, through lance-namespace (ADR 0171).

A media dataset is a catalog namespace and its tables are governed tables. Each is opened through the namespace with the
person's bearer (the media registry, :mod:`client_kit.lancekit.registry`), so pylance asks `DescribeTable` for the
credential the catalog vends that person and reads with it. The annotator holds no store credential, so there is no
other way in, and a table the person may not read is refused at the catalog.

A dataset's declared roles (row identity, the document binding) come from the `lance_media.descriptor` stamp on one of
its tables, checked against the tables before a route uses them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import lance
import lance_namespace
from pydantic import BaseModel, ConfigDict

from client_kit.lancekit.absence import reads_as_absent
from client_kit.lancekit.catalog import bounded, connect_namespace
from client_kit.lancekit.descriptor import Declared
from client_kit.lancekit.registry import DatasetHandle
from client_kit.media.state import dataset_handle
from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError
from service_kit.governed.token_exchange import LakehouseCaller


if TYPE_CHECKING:
    from client_kit.media.state import AppState


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
    return Corpus(id=handle.id, declared=handle.descriptor.declared, handle=handle)


def _is_table(client: lance_namespace.LanceNamespace, identifier: list[str]) -> bool:
    """Whether the catalog holds ``identifier`` as a table; a nested namespace of the same depth answers no.

    Asked with DescribeTable, which the spec answers TableNotFound for anything but a table, asking no credential (a
    classification reads no bytes) and bounded like every catalog call (`client_kit.lancekit.catalog.bounded`, whose
    timeout is a ServiceUnavailableError, which `input_tables` reads as a check that did not run). TableExists and
    NamespaceExists are not: measured 2026-11-14 on lance-namespace's ``dir`` catalog (pylance 13.0.0), each answers
    yes for a nested namespace's id and for a table's id alike.
    """
    try:
        bounded(lambda: client.describe_table(lance_namespace.DescribeTableRequest(id=identifier, vend_credentials=False)))
    except lance_namespace.TableNotFoundError:
        return False
    return True


def input_tables(state: AppState, names: list[str], caller: LakehouseCaller) -> dict[str, str | None] | None:
    """For each of ``names`` the catalog shows the caller, the governed table an item's captured version is a version of.

    A name is written as the catalog writes it, its warehouse first. One of three or more segments that the catalog holds
    as a table is a table, its own input; any other of two or more segments is a dataset (a namespace, possibly nested),
    whose items are rows of its default search's row table: the table whose version a sender captures (the zone's `rowTableVersion`), so that table at that
    version is what a publish of the items names as its input, which a target under the run rule requires. None for a dataset
    that declares no search. A name missing from the answer is one the caller cannot open on the canvas: absent,
    refused, or a dataset with no valid descriptor. None when the catalog could not be asked.
    """
    settings = state.settings
    client = connect_namespace(settings, caller.token)
    found: dict[str, str | None] = {}
    for name in names:
        identifier = name.split(settings.catalog_delimiter)
        try:
            if len(identifier) >= 3 and _is_table(client, identifier):
                found[name] = name
            else:
                search = resolve(state, name, caller).declared.search
                found[name] = settings.catalog_delimiter.join(settings.catalog_table_id(name, search.row_table)) if search else None
        except (
            lance_namespace.TableNotFoundError,
            lance_namespace.NamespaceNotFoundError,
            lance_namespace.PermissionDeniedError,
            NotFoundError,
            ForbiddenError,
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
    settings = state.settings
    client = connect_namespace(settings, caller.token)
    unheld: list[str] = []
    for table, version in sorted(captured):
        identifier = table.split(settings.catalog_delimiter)
        try:
            bounded(lambda i=identifier, v=version: lance.dataset(namespace_client=client, table_id=i, version=v))
        except (ValueError, OSError) as exc:
            if not reads_as_absent(exc):
                logger.warning("send could not read version %s of %s; not accepting a version it did not check", version, table)
                return None
            unheld.append(f"version {version} of {table}")
        except (lance_namespace.LanceNamespaceError, ServiceUnavailableError):
            logger.warning("send could not ask the catalog about %s; not accepting a version it did not check", table)
            return None
    return unheld
