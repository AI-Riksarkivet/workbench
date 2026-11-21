"""The workbench's own OpenFGA store: its model, how a service finds it, and the questions a door asks it.

The model (``auth/model.fga``, compiled to ``auth/model.json``) is the workbench's alone: organisations, projects with
their five concentric roles, and the tasks, corpora and flows that inherit from a project. Nothing here names an object
of a lakehouse's; a table read or write is the lakehouse's to decide on the caller's own credential.

The store and its model are written by the model writer the deployment runs (``workbench.governed.auth.write_model``),
never by a serving process. A service finds the store by its name and the model whose body its own image carries
(:func:`resolve`), read-only, so a rollout that changes the model takes effect for the pods built with it and no other.

Every read and write retries a transient failure (a transport error, OpenFGA 429 or 5xx) with jittered backoff inside
an overall budget, then fails CLOSED with a 503: an outage is never an allow and never a deny.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, cast, override
from urllib.parse import unquote

import aiohttp
from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientListObjectsRequest,
    ClientTuple,
    ClientWriteRequest,
    ClientWriteRequestOnDuplicateWrites,
    ClientWriteRequestOnMissingDeletes,
    ConflictOptions,
)
from openfga_sdk.configuration import RetryParams
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models.consistency_preference import ConsistencyPreference
from openfga_sdk.models.create_store_request import CreateStoreRequest
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
from openfga_sdk.models.write_authorization_model_request import WriteAuthorizationModelRequest
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception, stop_after_attempt, stop_after_delay, wait_exponential_jitter

from workbench.exceptions import ServiceUnavailableError, ValidationError
from workbench.governed.principal import Principal


log = logging.getLogger(__name__)

#: The store every workbench service and the model writer share, unless a deployment names another.
DEFAULT_STORE_NAME: Final = "workbench"
DEFAULT_TIMEOUT_SECONDS: Final = 5.0
_RETRY_ATTEMPTS: Final = 3
_RETRY_BACKOFF_SECONDS: Final = 0.2
_RETRY_MAX_BACKOFF_SECONDS: Final = 2.0
#: One logical call's retries may not pin a request for longer than this.
_RETRY_BUDGET_SECONDS: Final = 15.0
#: OpenFGA v1.18.3 serves at most 100 models, stores or tuples per page.
_PAGE_SIZE: Final = 100
#: The most tuples one OpenFGA write may carry (`maxTuplesPerWrite`, 100 by default); the server refuses a larger one.
MAX_TUPLES_PER_WRITE: Final = 100
#: A listing that has not ended after this many pages is refused rather than answered short.
_MAX_PAGES: Final = 400
#: How long a booting service waits for the model writer to have written its image's model.
RESOLVE_DEADLINE_SECONDS: Final = 120.0
#: How far apart a service looks again for the store and its model while they are not there.
RESOLVE_POLL_SECONDS: Final = 5.0

#: The aiohttp transport raises an unreachable server as a raw transport error, not an ``ApiException``.
_TRANSIENT_NETWORK: Final[tuple[type[BaseException], ...]] = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
_FAIL_CLOSED: Final[tuple[type[BaseException], ...]] = (ApiException, *_TRANSIENT_NETWORK)


class Grant(BaseModel):
    """One relationship tuple: ``user`` holds ``relation`` on ``object``. ``user`` is an OpenFGA user or object id."""

    model_config = ConfigDict(frozen=True)

    user: str
    relation: str
    object: str

    def tuple(self) -> ClientTuple:
        return ClientTuple(user=self.user, relation=self.relation, object=self.object)


def user_object(subject: str) -> str:
    """The OpenFGA user a principal key is (`Principal.fga_user`); a value that names no principal is a 400."""
    try:
        return Principal.parse(subject).fga_user
    except ValueError as exc:
        raise ValidationError(f"{subject!r} is not a principal: {exc}") from exc


def subject_of(user_id: str) -> str:
    """The principal key an OpenFGA ``user:`` id names: the inverse of :func:`user_object`."""
    return unquote(user_id.removeprefix("user:"))


def load_model() -> dict[str, Any]:
    """The model this image carries, as the store takes it."""
    return json.loads((files("workbench.governed.auth") / "model.json").read_text(encoding="utf-8"))


def _plain(value: Any) -> Any:
    """An SDK model (or any nesting of them) as plain data, in the wire's spelling (``serialize=True``)."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _plain(to_dict(serialize=True))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


#: Empty messages whose presence is the meaning (`this: {}` is a direct assignment, `wildcard: {}` is `[user:*]`).
_EMPTY_MESSAGES: Final = frozenset({"this", "wildcard"})
#: Map fields whose keys are an author's names, never grammar fields.
_NAMED_MAPS: Final = frozenset({"conditions", "parameters", "relations"})


def _without_defaults(value: Any, *, named: bool = False) -> Any:
    """Drop the empty values OpenFGA fills into a model it stores, keeping an empty message.

    The store does not hand back the body it was given: OpenFGA v1.18.3 fills `module: ""`, `source_info: null`,
    `directly_related_user_types: []` and the like, so a body is compared with those removed.
    """
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            stripped = _without_defaults(item, named=not named and key in _NAMED_MAPS)
            if (not named and key in _EMPTY_MESSAGES and stripped == {}) or stripped not in (None, "", {}, []):
                kept[key] = stripped
        return kept
    if isinstance(value, list):
        return [_without_defaults(item) for item in value]
    return value


def canonical_model(model: Any) -> str:
    """The comparable body of a model, from ``model.json`` or from the store: its schema, types and conditions."""

    def field(name: str) -> Any:
        return model.get(name) if isinstance(model, dict) else getattr(model, name, None)

    body = {name: _plain(field(name)) for name in ("schema_version", "type_definitions", "conditions")}
    return json.dumps(_without_defaults(body), sort_keys=True, separators=(",", ":"), default=repr)


class _FileCredential(CredentialConfiguration):
    """The OpenFGA credential read from its mounted secret file on every request the SDK sends.

    The SDK reads ``api_token`` each time it builds a request's ``Authorization`` header, so a rotated secret is
    presented by the next request; a copy taken at construction would stop authenticating at the first rotation.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    @property
    @override
    def api_token(self) -> str:
        try:
            token = Path(self._path).read_text().strip()
        except OSError as exc:
            raise ServiceUnavailableError(f"the OpenFGA credential at {self._path} could not be read") from exc
        if not token:
            raise ServiceUnavailableError(f"the OpenFGA credential at {self._path} is empty")
        return token

    @api_token.setter
    def api_token(self, value: str | None) -> None:
        raise AttributeError(f"the OpenFGA credential is read from {self._path} on every request and cannot be set")


def make_client(api_url: str, *, credential_file: str | None, store_id: str | None = None, model_id: str | None = None) -> OpenFgaClient:
    """An OpenFGA client presenting the credential in ``credential_file`` (none without one), its own retries off.

    Tenacity is the one retry layer (:func:`_guarded`); the SDK's own would multiply each attempt.
    """
    credentials = Credentials(method="api_token", configuration=_FileCredential(credential_file)) if credential_file else None
    return OpenFgaClient(
        ClientConfiguration(
            api_url=api_url,
            credentials=credentials,
            store_id=store_id,
            authorization_model_id=model_id,
            timeout_millisec=int(DEFAULT_TIMEOUT_SECONDS * 1000),
            retry_params=RetryParams(max_retry=0),
        )
    )


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_NETWORK):
        return True
    if isinstance(exc, ApiException):
        return exc.status in (0, None) or bool(exc.is_retryable())
    return False


async def _guarded[T](operation: Callable[[], Awaitable[T]], *, event: str) -> T:
    """Run one OpenFGA call: retry what is transient, then fail closed with a 503 whose message names no call."""
    retrying = retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(_RETRY_ATTEMPTS) | stop_after_delay(_RETRY_BUDGET_SECONDS),
        wait=wait_exponential_jitter(initial=_RETRY_BACKOFF_SECONDS, max=_RETRY_MAX_BACKOFF_SECONDS),
        reraise=True,
    )
    try:
        return await retrying(operation)()
    except _FAIL_CLOSED as exc:
        log.error(event, exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc


#: Every read asks past OpenFGA's caches, which a platform may turn on for scale: a cached answer may predate a revoke.
def _fresh(**options: Any) -> dict[str, Any]:
    return {"consistency": ConsistencyPreference.HIGHER_CONSISTENCY, **options}


async def _store_named(client: OpenFgaClient, name: str) -> str | None:
    """The id of the newest store named ``name`` across every page of the listing, or ``None``; a listing that does not
    end is a 503, never an absent store (which would make a writer create a second one)."""
    named: list[Any] = []
    token = ""
    for _ in range(_MAX_PAGES):
        options: dict[str, Any] = {"page_size": _PAGE_SIZE} | ({"continuation_token": token} if token else {})
        page = await client.list_stores(options=options)
        named.extend(store for store in page.stores or [] if store.name == name)
        token = page.continuation_token or ""
        if not token:
            return str(max(named, key=lambda store: store.created_at).id) if named else None
    raise ServiceUnavailableError(f"the store listing runs past {_MAX_PAGES} pages; refusing to read {name!r} as absent")


async def _model_carrying(client: OpenFgaClient, wanted: str) -> str | None:
    """The newest model in the client's store whose body is ``wanted``, or ``None``; a history too long to read is a 503."""
    token = ""
    for _ in range(_MAX_PAGES):
        options: dict[str, Any] = {"page_size": _PAGE_SIZE} | ({"continuation_token": token} if token else {})
        page = await client.read_authorization_models(options=options)
        for model in page.authorization_models or []:
            if canonical_model(model) == wanted:
                return str(model.id)
        token = page.continuation_token or ""
        if not token:
            return None
    raise ServiceUnavailableError(f"the store's model history runs past {_MAX_PAGES} pages; refusing to read it as absent")


async def provision(api_url: str, *, credential_file: str | None, store_name: str = DEFAULT_STORE_NAME) -> tuple[str, str]:
    """Make the store named ``store_name`` hold this image's model; return ``(store_id, model_id)``. Idempotent.

    The store is created when none carries the name, and the model written only when no model in it carries this body:
    every write mints a new immutable version, so writing an unchanged body would grow the history on every run.
    """
    model = load_model()
    async with make_client(api_url, credential_file=credential_file) as client:
        store_id = await _store_named(client, store_name)
        if store_id is None:
            store_id = str((await client.create_store(CreateStoreRequest(name=store_name))).id)
            log.info("openfga_store_created", extra={"store_name": store_name, "store_id": store_id})
    async with make_client(api_url, credential_file=credential_file, store_id=store_id) as client:
        held = await _model_carrying(client, canonical_model(model))
        if held is not None:
            log.info("openfga_model_unchanged", extra={"store_id": store_id, "model_id": held})
            return store_id, held
        written = await client.write_authorization_model(
            WriteAuthorizationModelRequest(
                schema_version=model["schema_version"], type_definitions=model["type_definitions"], conditions=model.get("conditions")
            )
        )
    log.info("openfga_model_written", extra={"store_id": store_id, "model_id": written.authorization_model_id})
    return store_id, str(written.authorization_model_id)


async def resolve(
    api_url: str,
    *,
    credential_file: str | None,
    store_name: str = DEFAULT_STORE_NAME,
    deadline_seconds: float = RESOLVE_DEADLINE_SECONDS,
    poll_seconds: float = RESOLVE_POLL_SECONDS,
) -> tuple[str, str] | None:
    """Find the store named ``store_name`` and the model this image carries in it, read-only; ``None`` past the deadline.

    A service never writes the model: on a first install the writer may not have run yet, so a missing store, model or
    server is waited out, ``poll_seconds`` apart. ``None`` leaves the service's doors answering 503.
    """
    wanted = canonical_model(load_model())
    loop = asyncio.get_running_loop()
    started = loop.time()
    reason = ""
    while True:
        try:
            async with make_client(api_url, credential_file=credential_file) as client:
                store_id = await _store_named(client, store_name)
            if store_id is None:
                reason = f"no store named {store_name!r}"
            else:
                async with make_client(api_url, credential_file=credential_file, store_id=store_id) as client:
                    model_id = await _model_carrying(client, wanted)
                if model_id is not None:
                    return store_id, model_id
                reason = f"store {store_id} holds no model carrying this image's model.json"
        except (*_FAIL_CLOSED, ServiceUnavailableError) as exc:
            reason = f"OpenFGA unavailable: {exc}"
        waited = loop.time() - started
        if waited + poll_seconds > deadline_seconds:
            log.warning("openfga_model_absent", extra={"store_name": store_name, "reason": reason, "waited_seconds": round(waited, 1)})
            return None
        await asyncio.sleep(poll_seconds)


class Authorizer:
    """The questions a workbench door asks its store, and the grants it writes, on one client pinned to store and model."""

    def __init__(self, client: OpenFgaClient) -> None:
        self._client = client

    async def check(self, *, user: str, relation: str, obj: str, contextual: Iterable[Grant] = ()) -> bool:
        """Whether the principal ``user`` holds ``relation`` on ``obj``, as if ``contextual`` were stored too.

        ``contextual`` carries a parent edge the caller holds from a trusted source (a task's project, from the task
        itself), so the check runs on the object's own permissions without a stored tuple per object.
        """
        request = ClientCheckRequest(user=user_object(user), relation=relation, object=obj, contextual_tuples=[g.tuple() for g in contextual] or None)

        async def _check() -> bool:
            return bool((await self._client.check(request, options=_fresh())).allowed)

        return await _guarded(_check, event="openfga_check_unavailable")

    async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        """``{object: allowed}`` for one principal across many objects, in one round trip; an unanswered item is a 503."""
        if not objects:
            return {}
        items = [ClientBatchCheckItem(user=user_object(user), relation=relation, object=o) for o in dict.fromkeys(objects)]

        async def _batch() -> dict[str, bool]:
            response = await self._client.batch_check(ClientBatchCheckRequest(checks=items), options=_fresh())
            if any(r.error is not None for r in response.result):
                raise ServiceUnavailableError("authorization service unavailable")
            return {r.request.object: bool(r.allowed) for r in response.result}

        return await _guarded(_batch, event="openfga_batch_check_unavailable")

    async def list_objects(self, *, user: str, relation: str, object_type: str) -> list[str]:
        """The objects of ``object_type`` the principal ``user`` holds ``relation`` on."""

        async def _list() -> list[str]:
            response = await self._client.list_objects(ClientListObjectsRequest(user=user_object(user), relation=relation, type=object_type), options=_fresh())
            return sorted(response.objects)

        return await _guarded(_list, event="openfga_list_objects_unavailable")

    async def grants_on(self, obj: str) -> list[Grant]:
        """Every tuple stored on ``obj``, all pages of them; a listing that does not end is a 503, never a short answer."""

        async def _read() -> list[Grant]:
            collected: list[Grant] = []
            token = ""
            for _ in range(_MAX_PAGES):
                options = _fresh(page_size=_PAGE_SIZE) | ({"continuation_token": token} if token else {})
                response = await self._client.read(ReadRequestTupleKey(object=obj), options=options)
                collected.extend(Grant(user=t.key.user, relation=t.key.relation, object=t.key.object) for t in response.tuples or [])
                token = response.continuation_token or ""
                if not token:
                    return collected
            raise ServiceUnavailableError(f"the tuples on {obj} run past {_MAX_PAGES} pages; refusing to answer with part of them")

        return await _guarded(_read, event="openfga_read_unavailable")

    async def write(self, grants: Iterable[Grant]) -> None:
        """Store ``grants`` in one transaction; one already stored is the post-condition holding, not an error."""
        tuples = [g.tuple() for g in grants]
        if not tuples:
            return
        options = {"conflict": ConflictOptions(on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE)}

        async def _write() -> None:
            await self._client.write(ClientWriteRequest(writes=tuples), options=cast("dict[str, Any]", options))

        await _guarded(_write, event="openfga_write_unavailable")

    async def delete(self, grants: Iterable[Grant]) -> None:
        """Remove ``grants`` in one transaction; one already absent is the post-condition holding, not an error."""
        tuples = [g.tuple() for g in grants]
        if not tuples:
            return
        options = {"conflict": ConflictOptions(on_missing_deletes=ClientWriteRequestOnMissingDeletes.IGNORE)}

        async def _delete() -> None:
            await self._client.write(ClientWriteRequest(deletes=tuples), options=cast("dict[str, Any]", options))

        await _guarded(_delete, event="openfga_delete_unavailable")

    async def close(self) -> None:
        with suppress(Exception):
            await self._client.close()


async def build_authorizer(
    api_url: str,
    *,
    credential_file: str | None,
    store_name: str,
    model_id: str | None,
    deadline_seconds: float = RESOLVE_DEADLINE_SECONDS,
    poll_seconds: float = RESOLVE_POLL_SECONDS,
) -> Authorizer | None:
    """The authorizer on the store named ``store_name``, pinned to ``model_id`` or to the model this image carries.

    ``None`` when the store or the model is not there by ``deadline_seconds``.
    """
    if model_id is None:
        resolved = await resolve(api_url, credential_file=credential_file, store_name=store_name, deadline_seconds=deadline_seconds, poll_seconds=poll_seconds)
        if resolved is None:
            return None
        store_id, model_id = resolved
    else:
        async with make_client(api_url, credential_file=credential_file) as client:
            found = await _store_named(client, store_name)
        if found is None:
            return None
        store_id = found
    log.info("openfga_resolved", extra={"store_id": store_id, "model_id": model_id})
    return Authorizer(make_client(api_url, credential_file=credential_file, store_id=store_id, model_id=model_id))


__all__ = [
    "DEFAULT_STORE_NAME",
    "Authorizer",
    "Grant",
    "build_authorizer",
    "canonical_model",
    "load_model",
    "make_client",
    "provision",
    "resolve",
    "subject_of",
    "user_object",
]
