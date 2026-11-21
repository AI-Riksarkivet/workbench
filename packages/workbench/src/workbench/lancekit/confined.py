"""A source that is not a lakehouse, read through a namespace that can only read, and only where git says.

A lakehouse vends each caller's store credential and decides every read. Any other source is a catalog (or a directory)
the workbench trusts for nothing but listing: measured 2026-11-16 on pylance 13.0.0 against a REST catalog on a socket,
a DescribeTable answer that names a store endpoint makes Lance sign its reads there with the process's own ambient
credentials (a moto store behind a key-recording proxy saw the ambient access key), and a storage option the reader
passes beside the namespace does not override the one the catalog answers. A Python ``LanceNamespace`` in front of the
catalog does govern it: pylance and lancedb both open through its ``describe_table`` and read on the storage options it
returns (measured the same day: an answer rewritten to a dead endpoint failed both opens).

So :class:`ConfinedNamespace` is the only client a non-lakehouse source is reached through. It answers the listings,
and a DescribeTable only for a name the source's own listing returned, with a location under one the declaration names,
and with the storage options the declaration names in place of whatever the catalog answered. Every other operation of
the spec (each write among them) is the base class's, which refuses it: the source is read-only by construction.
"""

from __future__ import annotations

import posixpath
from typing import Final
from urllib.parse import urlsplit

from lance_namespace import (
    DescribeTableRequest,
    DescribeTableResponse,
    InvalidInputError,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    PermissionDeniedError,
    TableNotFoundError,
)

from workbench.source_names import check_segments


#: The most pages one listing a describe confirms its name against may read, and the most names it may hold.
MAX_PAGES: Final = 100
MAX_NAMES: Final = 10_000


def _normal(location: str) -> tuple[str, str]:
    """A location as (scheme and authority, normalised path), so ``..`` cannot walk out of a prefix it starts under."""
    parts = urlsplit(location if "://" in location else f"file://{location}")
    return f"{parts.scheme}://{parts.netloc}", posixpath.normpath("/" + parts.path.lstrip("/"))


def within(location: str, roots: list[str]) -> bool:
    """Whether ``location`` lies at or under one of ``roots``."""
    where, path = _normal(location)
    for root in roots:
        root_where, root_path = _normal(root)
        if where == root_where and (path == root_path or path.startswith(root_path.rstrip("/") + "/")):
            return True
    return False


def _as[R: (ListNamespacesRequest, ListTablesRequest, DescribeTableRequest)](model: type[R], request: object) -> R:
    """``request`` as ``model``: pylance's storage-options provider calls a Python namespace with a dict carrying
    ``model_dump`` in place of the request model (pylance 13.0.0, measured 2026-11-16)."""
    if isinstance(request, model):
        return request
    dump = getattr(request, "model_dump", None)
    return model.model_validate(dump() if callable(dump) else request)


class ConfinedNamespace(LanceNamespace):
    """``inner``, read-only, its tables confined to what it lists, under ``roots``, read on ``storage``."""

    def __init__(self, inner: LanceNamespace, *, name: str, roots: list[str], storage: dict[str, str], only: str | None, delimiter: str) -> None:
        self._inner = inner
        self._name = name
        self._roots = list(roots)
        self._storage = dict(storage)
        self._only = only
        self._delimiter = delimiter

    def namespace_id(self) -> str:
        return f"confined:{self._name}"

    def _check(self, identifier: list[str] | None) -> list[str]:
        try:
            return check_segments(list(identifier or []), self._delimiter)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        request = _as(ListNamespacesRequest, request)
        self._check(request.id)
        return self._inner.list_namespaces(request)

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        request = _as(ListTablesRequest, request)
        self._check(request.id)
        answer = self._inner.list_tables(request)
        if self._only is not None:
            answer.tables = [name for name in answer.tables or [] if name == self._only]
        return answer

    def _listed(self, namespace: list[str], table: str) -> bool:
        token: str | None = None
        seen = 0
        for _ in range(MAX_PAGES):
            answer = self._inner.list_tables(ListTablesRequest(id=namespace, page_token=token))
            names = list(answer.tables or [])
            if table in names:
                return True
            seen += len(names)
            token = answer.page_token
            if not token or seen >= MAX_NAMES:
                return False
        return False

    def describe_table(self, request: DescribeTableRequest) -> DescribeTableResponse:
        request = _as(DescribeTableRequest, request)
        identifier = self._check(request.id)
        if not identifier:
            raise InvalidInputError("a table id names at least the table")
        namespace, table = identifier[:-1], identifier[-1]
        if (self._only is not None and table != self._only) or not self._listed(namespace, table):
            raise TableNotFoundError(f"table {table!r} is not one source {self._name!r} lists")
        answer = self._inner.describe_table(DescribeTableRequest(id=identifier, version=request.version, vend_credentials=False))
        if not answer.location or not within(answer.location, self._roots):
            raise PermissionDeniedError(f"source {self._name!r} answered a location outside the ones it is declared to read")
        answer.storage_options = dict(self._storage)
        return answer


__all__ = ["ConfinedNamespace", "within"]
