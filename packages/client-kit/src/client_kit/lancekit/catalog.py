"""A client's side of the catalog's doors, on the stock Lance Namespace SDK.

A client reaches the catalog through pylance's ``RestNamespace`` (what ``lance_namespace.connect("rest", ...)`` builds): the
Python SDK `lance_docs/ns_catalog/namespace/index.md` names for engines and frameworks; locally and in the offline suite
the same doors answer from lance-namespace's stock ``dir`` catalog (:func:`connect_namespace`). It depends on the
lakehouse only through that spec and the platform package (``service_kit``).

A table is read by opening it through the namespace (:mod:`client_kit.lancekit.registry`). A write goes through the
catalog's doors, :class:`NamespaceCatalogWriteTransport` (``merge_insert``, ``create`` and ``delete``), so the catalog
governs it (its authorization, quality gate and announcement on lineage). Every error the SDK raises is one of the spec's
typed errors, and :func:`translate_catalog_errors` maps its code onto the platform's DomainError hierarchy.

pylance 13.0.0's ``RestNamespace`` bounds no call (measured 2026-11-02): a catalog that accepts a connection and never
answers held a call past 45 s, with no setting and under every timeout key tried, and the REST page documents only
``uri``, ``delimiter`` and the ``header.*`` properties. So each call runs on :data:`_CALLS`, a small pool of its own (:func:`bounded`),
and its caller waits :data:`CALL_TIMEOUT_S` at most: a hung catalog costs a caller a 503, and holds at most the pool's
threads until it answers.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import lance_namespace
from lance.namespace import DynamicContextProvider, RestNamespace
from lance_namespace import (
    CreateTableRequest,
    DeleteFromTableRequest,
    ErrorCode,
    LanceNamespaceError,
    MergeInsertIntoTableRequest,
)
from opentelemetry import propagate

from service_kit.arrow_ipc import encode_arrow_stream
from service_kit.exceptions import ConflictError, DomainError, ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError, ValidationError
from service_kit.naming import CATALOG_DELIMITER


if TYPE_CHECKING:
    import pyarrow as pa
    from lance_namespace import LanceNamespace

    from client_kit.media.config import MediaSettings


log = logging.getLogger(__name__)

#: How long a caller waits on one catalog call, the bound the spec's generated client was given per request.
CALL_TIMEOUT_S: Final = 30.0
#: The threads catalog calls run on. Bounded so a catalog that holds its connections open holds at most these: further
#: calls wait in the queue and their callers time out, rather than each taking a thread of its own.
_CALLS: Final = ThreadPoolExecutor(max_workers=16, thread_name_prefix="catalog-call")

#: The spec's error codes a caller can act on, each as the platform error that carries it: the code-to-status table of
#: `lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md` § Error Code to HTTP Status Mapping. Every other code
#: (unsupported, unavailable, internal, throttled) is the catalog's trouble, not the caller's.
_BY_CODE: Final[dict[ErrorCode, tuple[type[DomainError], str]]] = {
    **dict.fromkeys(
        (
            ErrorCode.NAMESPACE_NOT_FOUND,
            ErrorCode.TABLE_NOT_FOUND,
            ErrorCode.TABLE_INDEX_NOT_FOUND,
            ErrorCode.TABLE_TAG_NOT_FOUND,
            ErrorCode.TRANSACTION_NOT_FOUND,
            ErrorCode.TABLE_VERSION_NOT_FOUND,
            ErrorCode.TABLE_COLUMN_NOT_FOUND,
            ErrorCode.TABLE_BRANCH_NOT_FOUND,
        ),
        (NotFoundError, "not found in the catalog"),
    ),
    **dict.fromkeys(
        (
            ErrorCode.NAMESPACE_ALREADY_EXISTS,
            ErrorCode.NAMESPACE_NOT_EMPTY,
            ErrorCode.TABLE_ALREADY_EXISTS,
            ErrorCode.TABLE_INDEX_ALREADY_EXISTS,
            ErrorCode.TABLE_TAG_ALREADY_EXISTS,
            ErrorCode.CONCURRENT_MODIFICATION,
            ErrorCode.INVALID_TABLE_STATE,
            ErrorCode.TABLE_BRANCH_ALREADY_EXISTS,
        ),
        (ConflictError, "catalog write conflict"),
    ),
    **dict.fromkeys((ErrorCode.INVALID_INPUT, ErrorCode.TABLE_SCHEMA_VALIDATION_ERROR), (ValidationError, "catalog rejected the request")),
    ErrorCode.PERMISSION_DENIED: (ForbiddenError, "catalog denied the request"),
    # 401 stays 401: a missing or invalid credential read as "you lack a grant" sends its reader after an FGA tuple that
    # was never the problem.
    ErrorCode.UNAUTHENTICATED: (UnauthorizedError, "catalog rejected the credential"),
}


def connect_catalog(base_url: str, *, token: str | None, delimiter: str = CATALOG_DELIMITER, headers: Mapping[str, str] | None = None) -> LanceNamespace:
    """The stock REST namespace for one caller: ``token`` rides every request as its bearer, and no other credential does.

    One per caller rather than one shared: the bearer is a header of the namespace, and a shared one would answer the next
    caller under the previous caller's identity. Measured 2026-11-02, a namespace and its describe cost 3.8 ms, and 200 of
    them left the process's thread count where it was. ``headers`` are the request's context headers (the spec's
    ``header.``-prefixed context, `lance_docs/ns_catalog/catalog/rest/index.md` § Context Header Mapping). The caller's
    trace context is added per call (:class:`TraceContext`), so the catalog's span joins the trace of the call that
    reached it: the SDK's client is Rust, which no Python instrumentor reaches.
    """
    properties = {"uri": base_url, "delimiter": delimiter} | {f"header.{name}": value for name, value in (headers or {}).items()}
    if token:
        properties["header.Authorization"] = f"Bearer {token}"
    return RestNamespace(context_provider=TraceContext(), **properties)


class TraceContext(DynamicContextProvider):
    """The trace context of the call being made, as headers, asked for before every namespace operation.

    Per call, not per client: a client outlives the request that built it (the registry caches it per caller), so a
    ``traceparent`` fixed at construction would put every later call in the first request's trace. pylance's
    ``RestNamespace`` asks its context provider before each operation and sends its ``headers.``-prefixed keys as HTTP
    headers (pylance 13.0.0, ``lance/namespace.py`` ``DynamicContextProvider``); the ``header.`` properties above are the
    client's static ones, the spelling ``RestNamespace``'s own docstring documents.
    """

    def provide_context(self, info: dict[str, str]) -> dict[str, str]:
        del info
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        return {f"headers.{name}": value for name, value in carrier.items()}


def connect_namespace(settings: MediaSettings, caller_token: str | None) -> LanceNamespace:
    """The namespace client the media plane reads and writes through, as the caller.

    The catalog's REST door on ``MEDIA_CATALOG_URI``, carrying the caller's bearer and no other credential; or
    lance-namespace's stock directory catalog on ``MEDIA_CATALOG_DIR``, which authenticates nobody, for development and
    the offline suite. Either way the same doors answer, so the code above this seam does not know which it has.
    """
    if settings.catalog_dir is not None:
        return lance_namespace.connect("dir", {"root": str(settings.catalog_dir)})
    if settings.catalog_uri is None:
        raise TypeError("MediaSettings names no catalog; its validator admits none without one")
    return connect_catalog(settings.catalog_uri, token=caller_token, delimiter=settings.catalog_delimiter)


@contextmanager
def translate_catalog_errors() -> Generator[None]:
    """Map the SDK's typed spec errors onto the platform's DomainError hierarchy, so a catalog refusal renders as the
    platform's problem+json instead of an opaque 500.

    A refusal keeps the catalog's own detail, which its handlers redact. Anything else is logged and answered with a stable
    sentence, since its responder may be an ingress or a sidecar rather than the catalog.
    """
    try:
        yield
    except LanceNamespaceError as exc:
        mapped = _BY_CODE.get(exc.code)
        if mapped is None:
            log.exception("catalog_unavailable", extra={"code": int(exc.code)})
            raise ServiceUnavailableError("the catalog is unavailable") from exc
        error, sentence = mapped
        raise error(f"{sentence}: {exc}") from exc


def bounded[T](call: Callable[[], T]) -> T:
    """``call`` on :data:`_CALLS`, its caller waiting :data:`CALL_TIMEOUT_S` at most.

    The call runs in a copy of the caller's context, so what it reads of the caller's context variables (the current
    span, which :class:`TraceContext` sends on) is the caller's, not the pool thread's.
    """
    context = contextvars.copy_context()
    future: Future[T] = _CALLS.submit(lambda: context.run(call))
    try:
        return future.result(timeout=CALL_TIMEOUT_S)
    except TimeoutError as exc:
        raise ServiceUnavailableError(f"the catalog did not answer within {CALL_TIMEOUT_S:.0f} s") from exc


class NamespaceCatalogWriteTransport:
    """The catalog's write doors for one table, through the stock SDK (:meth:`client_kit.lancekit.registry.DatasetHandle.writer`)."""

    def __init__(self, namespace: LanceNamespace, table_id: list[str]) -> None:
        self._namespace = namespace
        self._id = list(table_id)

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        """Insert the unmatched rows of ``delta`` and update the matched ones, keyed on ``on``."""
        self._merge(delta, MergeInsertIntoTableRequest(id=self._id, on=on, when_matched_update_all=True, when_not_matched_insert_all=True))

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        """Insert the unmatched rows of ``delta``, leaving a matched row as it is."""
        self._merge(delta, MergeInsertIntoTableRequest(id=self._id, on=on, when_matched_update_all=False, when_not_matched_insert_all=True))

    def create(self, data: bytes, *, mode: str, properties: Mapping[str, str]) -> int | None:
        """Create the table from an Arrow IPC stream body, answering the version the catalog committed.

        ``mode="exist_ok"`` keeps a table that already exists and answers its version, which is what makes a replayed create
        safe. ``properties`` reach the catalog as the spec's JSON-encoded query parameter.
        """
        request = CreateTableRequest(id=self._id, mode=mode, properties=dict(properties))
        with translate_catalog_errors():
            response = bounded(lambda: self._namespace.create_table(request, data))
        return response.version

    def delete(self, predicate: str) -> None:
        """Delete the rows matching ``predicate``."""
        request = DeleteFromTableRequest(id=self._id, predicate=predicate)
        with translate_catalog_errors():
            bounded(lambda: self._namespace.delete_from_table(request))

    def _merge(self, delta: pa.Table, request: MergeInsertIntoTableRequest) -> None:
        body = encode_arrow_stream(delta)
        with translate_catalog_errors():
            bounded(lambda: self._namespace.merge_insert_into_table(request, body))
