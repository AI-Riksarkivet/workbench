"""The lakehouse side of the publish saga — the concrete :class:`~annotator.projects.saga.Publisher`.

Posts through the catalog's REST API over the stock Lance Namespace SDK, the stack the media plane's
merge transport uses (`client_kit.lancekit.catalog`) — **the annotator never writes Lance directly**
(§7.1): the catalog is what places the new table in its namespace's authorization graph and emits the
``CREATE`` RunEvent.

Retry safety maps onto the catalog's own contract: ``create`` posts ``mode=exist_ok``. Fresh id ⇒ the
catalog creates and emits the event. Replayed id ⇒ the catalog KEEPS the existing table and returns its
version — exactly the "existing table with this id is success" the saga relies on.

The publish tags nothing. A tag in the lakehouse is a publication pointer, created and moved by an owner
or a publisher, and the workbench's identity is neither: a service is never made the owner of what it
creates, and its grant is writer on the target. The version the create returns, which the project records
and whose commit names the publish as its run, is the publish's reference; blessing it is the tenant's act
at the catalog's publish door.

A create the catalog does not answer is sent again (:data:`CREATE_ATTEMPTS`), which ``mode=exist_ok``
makes safe.

**The saga publishes as the workbench's own identity**: a token addressed to the lakehouse that the
workbench's confidential client takes with the client-credentials grant, after the annotator's own
`can_publish` check passed. The lakehouse decides whether that identity may write the target, and
``x-lance-originator`` names the person the publish is for, so the commit's run names them. The saga
outlives the person's request, so it never acts on their token.

:class:`PublishRunner` drives the saga: the `publish` door hands it the project once the transition commits, and each
replica's re-drive claims any publishing project whose run stopped. A run holds a lease on its project's row, so one
run at a time drives a publish across every replica.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from lance_namespace import DescribeNamespaceRequest, LanceNamespace

from annotator.core.config import get_annotator_settings
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA, PublishPlan, PublishRefusal, publish_namespace
from annotator.projects.saga import LostLease, Publisher, PublishOutcome, run_publish
from client_kit.lancekit.catalog import NamespaceCatalogWriteTransport, connect_catalog, translate_catalog_errors
from client_kit.sources import SOURCE_SEPARATOR
from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError
from service_kit.governed.token_exchange import LakehouseTokens
from service_kit.naming import CATALOG_DELIMITER


if TYPE_CHECKING:
    from annotator.state.store import AnnotatorStore


logger = logging.getLogger(__name__)

#: The catalog Context headers that name a write's run and its inputs (ADR 0156 C2): the run is the commit's OpenLineage
#: parent, a UUID run id with its job's namespace and name (ADR 0191).
RUN_ID_HEADER = "x-rask-run-id"
JOB_NAMESPACE_HEADER = "x-rask-job-namespace"
JOB_HEADER = "x-rask-job"
INPUTS_HEADER = "x-rask-inputs"
#: The namespace property under which the catalog requires every data write beneath to name its run and inputs.
REQUIRES_RUN_PROPERTY = "rask.requires_run"
#: The job a publish is a run of.
PUBLISH_JOB = ("annotator", "annotate.publish")

#: Attempts at the publish's create before it fails the saga run. A dropped connection or an unanswered call is retried
#: here rather than failing the publish, and retrying is safe by construction, not by hope: `create` posts
#: `mode=exist_ok`, the property this module's retry-safety argument rests on. pylance 13.0.0's REST client retries
#: nothing itself (measured 2026-11-02: one attempt against a reset connection, then an error).
CREATE_ATTEMPTS = 3
#: The pause before the second attempt, doubled before each later one.
CREATE_BACKOFF_S = 0.5


def _arrow_stream_bytes(plan: PublishPlan) -> bytes:
    """The plan's rows as an Arrow-IPC stream body for the catalog's create. Explicit schema, so an
    all-sentinel publish still produces correctly-typed columns."""
    import pyarrow as pa  # noqa: PLC0415 - heavy import, publish path only

    from service_kit.arrow_ipc import encode_arrow_stream  # noqa: PLC0415 - keeps pyarrow off module import

    return encode_arrow_stream(pa.Table.from_pylist(plan.rows, schema=PUBLISHED_LABELS_SCHEMA))


class CatalogPublisher:
    """`saga.Publisher` over the catalog's REST API, through the stock Lance Namespace SDK.

    The create is a blocking SDK call, so it runs in a worker thread; a namespace is built per call, carrying that call's
    bearer and context headers."""

    def __init__(
        self,
        base_url: str,
        *,
        source: str,
        delimiter: str = CATALOG_DELIMITER,
        token: Callable[[], str] | None = None,
        originator: str | None = None,
    ) -> None:
        self._source = source
        self._base_url = base_url
        self._delimiter = delimiter
        self._token = token
        self._originator = originator

    @property
    def lakehouse(self) -> str:
        return self._source

    def _in_lakehouse(self, table: str) -> list[str]:
        """A captured input's lakehouse id: its qualified name (``<source>~<id>``, `client_kit.sources`) without the source;
        the saga passes only the lakehouse's own tables as inputs (`annotator.projects.publish.publish_inputs`).

        Raises:
            PublishRefusal: The input names another source.
        """
        source, separator, identifier = table.partition(SOURCE_SEPARATOR)
        if not separator or source != self._source:
            raise PublishRefusal(f"input {table!r} is not a table of the lakehouse {self._source!r}")
        return identifier.split(self._delimiter)

    def _headers(self) -> dict[str, str]:
        """This call's static headers: the person the publish is for.

        The token is the workbench's own credential and the catalog authors every resulting event as that identity, so
        the originator is the only place the person survives. It authorizes nothing, which is why it may ride beside a
        service's token.
        """
        return {"x-lance-originator": self._originator} if self._originator else {}

    async def create_table(
        self,
        table_id: str,
        plan: PublishPlan,
        *,
        properties: dict[str, str],
        run_id: str,
        inputs: list[tuple[str, int]],
    ) -> int:
        """Create (or converge on) the published table; return its version."""
        body = await asyncio.to_thread(_arrow_stream_bytes, plan)
        headers = {**self._headers(), RUN_ID_HEADER: run_id, JOB_NAMESPACE_HEADER: PUBLISH_JOB[0], JOB_HEADER: PUBLISH_JOB[1]}
        if inputs:
            headers[INPUTS_HEADER] = json.dumps([{"table": self._in_lakehouse(table), "version": version} for table, version in inputs])
        catalog = NamespaceCatalogWriteTransport(
            connect_catalog(self._base_url, token=self._token, delimiter=self._delimiter, headers=headers),
            table_id.split(self._delimiter),
        )
        version = await asyncio.to_thread(_create_with_retries, catalog, body, properties)
        return version or 1

    async def requires_run(self, namespace: str) -> bool:
        """Whether any namespace on ``namespace``'s path, the warehouse first, declares the run rule, read as the catalog
        reads it (`docs/rask/WRITE-GOVERNANCE.md`): ``rask.requires_run = "true"`` among its properties. A namespace that
        does not exist declares nothing; the create then answers its own refusal.

        Every row on the path is one the workbench's client may describe once it holds a grant on the target: the
        lakehouse lets a grant beneath a warehouse or namespace see that container (`can_get_metadata` is
        `reader_effective or visible_below from child` on both, rask `packages/lakehouse/src/lakehouse/governed/auth/
        model.fga` at `f9c2c4e02`), and serves a describe on a warehouse's own one-segment name.

        Raises:
            PublishRefusal: The catalog refused to describe a namespace on the path, so whether the target requires a run
                cannot be told; the publish is refused rather than guessed.
        """
        path = namespace.split(self._delimiter)
        client = connect_catalog(self._base_url, token=self._token, delimiter=self._delimiter, headers=self._headers())
        return await asyncio.to_thread(_declares_run_rule, client, path, self._delimiter)


def _declares_run_rule(client: LanceNamespace, path: list[str], delimiter: str) -> bool:
    for depth in range(1, len(path) + 1):
        try:
            with translate_catalog_errors():
                described = client.describe_namespace(DescribeNamespaceRequest(id=path[:depth]))
        except NotFoundError:
            continue
        except ForbiddenError as exc:
            namespace = delimiter.join(path[:depth])
            raise PublishRefusal(
                f"the catalog does not let the workbench read namespace {namespace}, so whether a publish into it must name its run "
                "cannot be told: grant the workbench's client a role on the publish target"
            ) from exc
        if (described.properties or {}).get(REQUIRES_RUN_PROPERTY) == "true":
            return True
    return False


def _create_with_retries(catalog: NamespaceCatalogWriteTransport, body: bytes, properties: dict[str, str]) -> int | None:
    """The create, sent again after an unavailable answer until :data:`CREATE_ATTEMPTS` are spent."""
    for attempt in range(1, CREATE_ATTEMPTS):
        try:
            return catalog.create(body, mode="exist_ok", properties=properties)
        except ServiceUnavailableError:
            logger.warning("the catalog did not take the publish's create (attempt %d of %d); sending it again", attempt, CREATE_ATTEMPTS)
            time.sleep(CREATE_BACKOFF_S * 2 ** (attempt - 1))
    return catalog.create(body, mode="exist_ok", properties=properties)


# --------------------------------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------------------------------


#: Makes the publisher a run posts through, for the person the publish is for; `None` when there is no catalog to post to.
type PublisherFactory = Callable[[str], Publisher | None]


class PublishRunner:
    """Drives publishes in this process, each under a lease on its project's row (`AnnotatorStore.claim_publish`).

    A run starts when the publish door's transition commits (`spawn`), or when this replica's re-drive (`start`, every
    `redrive_seconds` with jitter) finds a publishing project whose lease is absent or expired: one whose run finished
    without ending the publish, was cancelled at a shutdown, or died with its process. A run renews its lease between
    the saga's steps and releases it when it ends, and holds no connection between store calls. At most `concurrency`
    runs go at once in one replica; a publish beyond that waits for the next sweep, here or in another replica.

    Two runs of one publish can overlap only when a lease lapsed under a step slower than the lease: the saga's
    idempotency by publish token makes that safe (`annotator.projects.saga`).
    """

    def __init__(
        self,
        store: AnnotatorStore,
        tokens: LakehouseTokens | None,
        *,
        lease_seconds: int,
        redrive_seconds: float,
        concurrency: int,
        publisher_for: PublisherFactory | None = None,
    ) -> None:
        self._store = store
        self._tokens = tokens
        self._lease_seconds = lease_seconds
        self._redrive_seconds = redrive_seconds
        self._concurrency = concurrency
        self._publisher_for = publisher_for or self._catalog_publisher
        #: The name this process's runs claim leases under.
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        #: The runs in flight, by project: strong references (the event loop holds only a weak one) and the count the
        #: concurrency bound reads.
        self._runs: dict[str, asyncio.Task[None]] = {}
        #: Slots a sweep has promised to the runs it is claiming.
        self._reserved = 0
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start this replica's re-drive: it sweeps at once, then every `redrive_seconds` with jitter."""
        self._loop_task = asyncio.get_running_loop().create_task(self._redrive())

    async def close(self) -> None:
        """Stop the re-drive, cancel the runs in flight and wait for them. Each releases its lease on the way out, so
        another replica claims its publish at its next sweep."""
        tasks = [*self._runs.values(), *([self._loop_task] if self._loop_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def spawn(self, project_id: str) -> asyncio.Task[None] | None:
        """Start a run for the project the door just moved to `publishing`, if this replica has room; otherwise, or
        when another run holds its lease, the publish is left to a sweep."""
        if project_id in self._runs or self._busy() >= self._concurrency:
            return None
        return self._track(project_id, self._claim_and_drive(project_id))

    async def sweep(self) -> list[str]:
        """Claim and start runs for the publishing projects no live run holds, up to this replica's free room."""
        free = self._concurrency - self._busy()
        if free <= 0:
            return []
        self._reserved += free
        try:
            claimed = await self._store.claim_publish(self.owner, self._lease_seconds, limit=free)
        finally:
            self._reserved -= free
        for project_id in claimed:
            if project_id not in self._runs:
                self._track(project_id, self._drive(project_id))
        return claimed

    def _busy(self) -> int:
        return len(self._runs) + self._reserved

    def _track(self, project_id: str, run: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(run)
        self._runs[project_id] = task
        task.add_done_callback(lambda _done: self._runs.pop(project_id, None))
        return task

    async def _redrive(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("the publish re-drive could not sweep; it tries again at its next tick")
            await asyncio.sleep(self._redrive_seconds * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, not secrecy

    async def _claim_and_drive(self, project_id: str) -> None:
        if not await self._store.claim_publish(self.owner, self._lease_seconds, project_id=project_id):
            logger.debug("a publish of %s is held by another run; this one stands down", project_id)
            return
        await self._drive(project_id)

    async def _drive(self, project_id: str) -> None:
        """Run one claimed publish to its end, then give its lease up, whatever ended it."""

        async def renew() -> None:
            if not await self._store.renew_publish(project_id, self.owner, self._lease_seconds):
                raise LostLease(f"the publish lease on project {project_id} is no longer {self.owner}'s")

        try:
            outcome = await self.run(project_id, renew=renew)
            if outcome is not None:
                logger.info(
                    "published project %s: table %s v%d (%d rows, %d shapes)%s",
                    outcome.project_id,
                    outcome.table_id,
                    outcome.version,
                    outcome.rows,
                    outcome.shapes,
                    " [converged]" if outcome.already_published else "",
                )
        except LostLease:
            logger.info("the publish of project %s moved to another run", project_id)
        except Exception:
            # `run_publish` already fired `publish_failed` with the reason, a resting state the publish door retries
            # from. This log is the operator-facing trace.
            logger.exception("publish saga failed for project %s", project_id)
        finally:
            try:
                await self._store.release_publish(project_id, self.owner)
            except Exception:
                # The lease then lapses at its expiry instead; whatever ended the run above stays what is reported.
                logger.warning("could not release the publish lease on project %s; it lapses at its expiry", project_id, exc_info=True)

    async def run(self, project_id: str, *, renew: Callable[[], Awaitable[None]] | None = None) -> PublishOutcome | None:
        """Run the saga for one project.

        Reads the pinned target namespace and the person the publish is for off the project — the door authorized THAT
        namespace, so this never guesses one.
        """
        project = await self._store.get_project(project_id)
        if project is None:
            logger.warning("a publish of project %s was started, and the project does not exist", project_id)
            return None
        namespace = project.pending_target_namespace or publish_namespace(project.publish_warehouse, get_annotator_settings().publish_namespace)
        subject = project.pending_publish_by or "system"

        publisher = self._publisher_for(subject)
        if publisher is None:
            # No transport is a CONFIGURATION failure, and it surfaces as one: `publish_failed` with a reason the UI can
            # show, rather than a project left `publishing` against a catalog that was never named.
            try:
                await self._store.fire_project(
                    project_id,
                    event="publish_failed",
                    actor=subject,
                    error="no rask lakehouse source is read over its REST door, so a publish has no catalog to post to",
                )
            except Exception:
                logger.exception("could not record the missing-catalog failure for project %s", project_id)
            return None
        return await run_publish(store=self._store, project_id=project_id, publisher=publisher, namespace=namespace, subject=subject, renew=renew)

    def _catalog_publisher(self, subject: str) -> Publisher | None:
        """The publisher over the catalog's REST door, as the workbench's own confidential client (its token asked per
        call; one that cannot be had fails the call it was for, which surfaces as `publish_failed` with the reason)."""
        lakehouse = get_annotator_settings().sources.lakehouse
        if lakehouse is None or lakehouse.uri is None:
            return None
        return CatalogPublisher(
            lakehouse.uri,
            source=lakehouse.name,
            delimiter=lakehouse.delimiter,
            token=self._tokens.for_service if self._tokens is not None else None,
            originator=subject,
        )
