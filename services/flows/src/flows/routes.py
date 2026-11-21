"""The flows HTTP surface: `/flows/catalog`, and per project `/flows/projects/{project}/validate` and `.../runs[/{run_id}]`.

Mounted under `settings.api_prefix` by `make_service_app`, so the deployed paths are
`/api/flows/...` (the chart sets `WORKBENCH_API_PREFIX=/api`, and `scripts/local-stack.sh` matches it) —
which is exactly what the cluster route's `/api/flows` rule forwards unrewritten. A router prefix of
`/flows` with an api_prefix of `/api/v1` would serve `/api/v1/flows/...` and every call through the
route would 404.

Every project route is gated on that project's roles in the workbench's own store (`flows.security`), and a run id is
DERIVED from the caller's `Idempotency-Key` rather than minted per attempt — the run id is the run row's key, so
deriving it is what makes a retry converge on the run it is retrying instead of forking a second one. See `create_run`.
"""

import logging
import uuid
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Header, Path, Query, Request, Response

from flows import executor, security
from flows.audit_actions import FlowsAuditAction
from flows.catalog import CATALOG
from flows.dependencies import FlowsSettingsDep, HttpDep, RunnerDep, StoreDep
from flows.graph import validate_graph
from flows.models import (
    CatalogResponse,
    FlowGraph,
    IdempotencyKeyReusedError,
    InferenceTooLargeError,
    RunJob,
    RunRefused,
    RunRefusedError,
    RunRequest,
    RunState,
    ServeAppHasNoIngressError,
    ServeUpstreamError,
    ValidateResponse,
)
from flows.serve_auth import ServeCredentialError, serve_authorization
from workbench.exceptions import NotFoundError, ServiceUnavailableError
from workbench.exceptions import ValidationError as BadRequestError


log = logging.getLogger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])

#: A project id, as the annotator mints and names them.
ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
#: A run id, as `run_id_for` derives them: anything else names no run and never reaches the store.
RunId = Annotated[str, Path(pattern=r"^run-[0-9a-f]{32}$")]

#: The namespace that makes a run id deterministic across processes, replicas and restarts. A fixed
#: constant, not uuid1/uuid4: the whole point is that the same `Idempotency-Key` yields the same run
#: id — and therefore the same run row — on a different pod, after a crash, on a retry.
RUN_NAMESPACE = uuid.UUID("2c9b7f41-6d4a-5e18-9a3c-71b0c5d2e480")


def run_id_for(project: str, subject: str, idempotency_key: str) -> str:
    """Derive the run id from the CALLER's key.

    Deterministic by construction: same subject + same key -> same id, on any pod, after any crash.
    A token minted per attempt leaves one orphan run per retry, and it is what made a schedule
    timeout unrecoverable: with nothing to converge on, the only choices were "run it again" and
    "lose it".

    Scoped by PROJECT and SUBJECT, because the run id IS the run row's key and it is also the URL of a
    readable resource: an unscoped key would let one caller's `Idempotency-Key: 1` land on another's
    run, or one project's on another's.

    The name is INJECTIVE, and a printable separator is not enough to make it so. `f"{subject}-flows-{key}"`
    lets the delimiter occur inside either field, so distinct pairs collide: subject `alice` with
    `Idempotency-Key: b-flows-c` and subject `alice-flows-b` with key `c` both render
    `alice-flows-b-flows-c` and therefore the same uuid5 — measured, not reasoned:
    both produced `run-ad6bec6da0b75e49a3a396c284f65ae4`. The key half is a caller-supplied HTTP
    header, so one crafted subject is enough to sit on another caller's run: read their node outputs
    at a URL they now share, or have their POST answered as a duplicate of someone else's.

    `\\x00` is the separator because it cannot appear in either field — an HTTP header value cannot
    carry a NUL, and a token `sub` is a JSON string that would have to smuggle one through the
    verifier. That is a property of the alphabet rather than a convention about what people name
    things, which is the only kind of separator argument that survives a hostile input.
    """
    return f"run-{uuid.uuid5(RUN_NAMESPACE, '\x00'.join((project, subject, 'flows', idempotency_key))).hex}"


@router.get("/catalog", dependencies=[security.AUTHENTICATED])
async def get_catalog() -> CatalogResponse:
    """The server-declared node kinds, to any verified person: they name no project and no run. Shape is pinned —
    see `models.CatalogResponse`."""
    return CatalogResponse(kinds=CATALOG)


@router.post("/projects/{project}/validate")
async def validate(
    project: ProjectId,
    graph: FlowGraph,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> ValidateResponse:
    """Graph hygiene, with no cluster involved: duplicate ids, unknown kinds, dangling edges,
    self-loops, cycles. Same vocabulary the frontend executor refuses on.

    GATED: every door is authenticated. The graph is also bounded at
    the MODEL now — `FlowGraph.nodes` carries `max_length` — because the ceiling in `validate_graph`
    fires only after every node has been built, which bounded the answer and not the work.
    """
    await security.require(checker, subject, security.READ, project, FlowsAuditAction.FLOWS_READ)
    problems = validate_graph(graph)
    return ValidateResponse(ok=not problems, problems=problems)


@router.post(
    "/projects/{project}/runs",
    status_code=HTTPStatus.ACCEPTED,
    # NO `response_model=`: the return annotation is `RunState` and FastAPI derives the schema from
    # it, so restating it would carry no information. Ruff's FAST001 refuses the restatement here.
    responses={
        HTTPStatus.OK: {"model": RunState, "description": "A replay: the Idempotency-Key names a run already started."},
        HTTPStatus.UNPROCESSABLE_ENTITY: {"model": RunRefused, "description": "The graph does not validate, or the Idempotency-Key names another request."},
    },
)
async def create_run(
    project: ProjectId,
    request: RunRequest,
    response: Response,
    settings: FlowsSettingsDep,
    store: StoreDep,
    runner: RunnerDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    # Bounded like Stripe's (255): the key is only hashed into the run id, and a bound keeps what a caller may send short.
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> RunState:
    """Start a flow run: 202 with the run as recorded, driven to its end in the background (`flows.runner`); 200 and
    `Idempotent-Replayed: true` with the run as it stands when the Idempotency-Key names one already started.

    Validation first, always: an invalid graph is refused with the problem LIST rather than half-executed. The run is
    then recorded in the service's own Postgres before it is answered, so an accepted run survives this process: any
    replica's re-drive resumes it at the first step with no outcome.

    `Idempotency-Key` makes a RETRY safe: the run id is DERIVED from it (`run_id_for`), and the row's key is the run id,
    so a retried or concurrent start with that key finds the run the first one recorded and starts nothing. A key-less
    POST gets a fresh run: without a caller key there is nothing to converge on.
    """
    problems = validate_graph(request.graph)
    if problems:
        raise RunRefusedError(f"{len(problems)} problem(s) in the graph", extensions={"problems": problems})

    # AUTHZ, after shape (identity 401 → shape 422 → authz 403). A flow's model nodes invoke live Serve endpoints, so
    # this is the route that spends compute, and it takes the project's run role.
    await security.require(checker, subject, security.RUN, project, FlowsAuditAction.FLOWS_RUN)

    # A key-less call gets a fresh run: `run_id_for` still derives it, so one code path mints every
    # run id, and the randomness lives in the KEY rather than in a second id scheme.
    run_id = run_id_for(project, subject, idempotency_key or uuid.uuid4().hex)
    job = RunJob(
        graph=request.graph,
        seeds=request.seeds,
        serve_url=settings.serve_url,
        serve_timeout=settings.serve_timeout,
        serve_token_file=settings.serve_token_file,
        project=project,
    )
    run, created = await store.create(run_id, subject=subject, job=job)
    if created:
        runner.spawn(run_id)
        return run.as_state()
    # A REPLAY. The key names a run already recorded, so nothing starts: the answer is that run as it stands, said to be a
    # replay — 200 rather than the fresh accept's 202, and `Idempotent-Replayed: true`, the header Stripe's API set as
    # the convention. The same key on a different request is the caller's mistake, refused rather than answered with a
    # run it did not ask for (draft-ietf-httpapi-idempotency-key-header § 2.7: 422).
    if run.request_digest != request.digest():
        raise IdempotencyKeyReusedError(f"the Idempotency-Key already names run {run_id}, started with another graph or other seeds")
    response.status_code = HTTPStatus.OK
    response.headers["Idempotent-Replayed"] = "true"
    return run.as_state()


@router.get("/projects/{project}/runs/{run_id}")
async def get_run(
    project: ProjectId,
    run_id: RunId,
    store: StoreDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> RunState:
    """Read a run back: its state and each recorded step's outcome.

    A run document carries what the model nodes returned — transcribed page text, prompt output, upstream error
    strings — so it takes the project's read role (a viewer reads a flow's runs). AUTHZ BEFORE EXISTENCE: a 404 that
    only a permitted caller can distinguish from a 403 is one less oracle for guessing run ids, and a run of another
    project is a 404 here.
    """
    await security.require(checker, subject, security.READ, project, FlowsAuditAction.FLOWS_RUN_READ)
    run = await store.get(run_id)
    if run is None or run.project != project:
        raise NotFoundError(f"no such run: {run_id}")
    return run.as_state()


@router.post("/projects/{project}/runs/{run_id}/cancel")
async def cancel_run(
    project: ProjectId,
    run_id: RunId,
    store: StoreDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> RunState:
    """Stop a run: it ends `cancelled` at once, and no step it has not started is executed.

    A step already in flight runs to its end, and its outcome is not recorded: its driver finds the run ended at its
    next record and stops. A run that already ended keeps its outcome, and the answer is the run as it stands.

    THE SAME relation as start: whoever may spend compute on a project's graph may stop spending it, so the person
    who started a runaway can stop it. AUTHZ BEFORE EXISTENCE, matching `get_run`.
    """
    await security.require(checker, subject, security.RUN, project, FlowsAuditAction.FLOWS_RUN_CANCEL)
    cancelled = await store.cancel(run_id, project)
    if cancelled is None:
        raise NotFoundError(f"no such run: {run_id}")
    return cancelled.as_state()


#: The most bytes one inference carries: a page scan is a few MB.
MAX_INFER_BYTES = 25 * 1024 * 1024
#: The most bytes of Serve's answer this door relays; an ALTO page or a chat completion is far below it.
MAX_INFER_ANSWER_BYTES = 25 * 1024 * 1024

#: A path within a Serve app: slash-separated segments of URL-safe characters, so the door reaches only the app it
#: names. An OpenAI-compatible app answers under `/v1/...`, which a bare app route cannot reach.
SERVE_PATH = r"^(/[A-Za-z0-9._~-]+)*/?$"

ServeApp = Annotated[str, Query(pattern=executor.APP_PATTERN.pattern, max_length=63, description="The Serve app's route prefix.")]
ServePath = Annotated[str, Query(pattern=SERVE_PATH, max_length=512, description="The path within the app.")]
PageName = Annotated[str, Query(max_length=256, pattern=r"^[^\x00-\x1f\x7f]*$", description="The page key Serve stamps into what it answers.")]


async def _bounded_body(request: Request) -> bytes:
    """The request body, refused past :data:`MAX_INFER_BYTES` by its declared length or by what it carries."""
    if int(request.headers.get("content-length") or 0) > MAX_INFER_BYTES:
        raise InferenceTooLargeError(f"the body is larger than {MAX_INFER_BYTES // (1024 * 1024)} MB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INFER_BYTES:
            raise InferenceTooLargeError(f"the body is larger than {MAX_INFER_BYTES // (1024 * 1024)} MB")
    if not body:
        raise BadRequestError("the request carries no bytes to infer on")
    return bytes(body)


@router.post("/projects/{project}/infer")
async def infer(
    project: ProjectId,
    request: Request,
    http: HttpDep,
    settings: FlowsSettingsDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    app: ServeApp = "htrflow",
    path: ServePath = "",
    name: PageName = "",
) -> Response:
    """Run one payload through a live Ray Serve app, for the studio's sandbox: bytes in, the app's answer out.

    It spends compute, so it takes the project's run role, as starting a flow does. Serve is called as this service, with
    its own projected ServiceAccount token (`flows.serve_auth`), at its own ingress (`WORKBENCH_FLOWS_SERVE_URL`), and
    only at the app and path the query names, each held to its alphabet with no ``.`` or ``..`` segment, so the door
    cannot reach anything else on the Serve host. Serve's answer is relayed with its own type and
    ``X-Content-Type-Options: nosniff``, read in full up to :data:`MAX_INFER_ANSWER_BYTES` before it is relayed.

    Refusals: 413 past the size ceiling, 400 on an empty body, 503 when this service has no Serve credential or Serve
    cannot be reached, 501 when the app has no HTTP ingress (Serve answers 405), and 502 on any other error Serve
    answers or an answer past the ceiling.
    """
    await security.require(checker, subject, security.RUN, project, FlowsAuditAction.FLOWS_INFER)
    if any(segment in (".", "..") for segment in path.split("/")):
        raise BadRequestError("a Serve app path may not hold a '.' or '..' segment")
    try:
        credential = serve_authorization(settings.serve_token_file)
    except ServeCredentialError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    body = await _bounded_body(request)
    url = f"{settings.serve_url.rstrip('/')}/{app}{path}"
    headers = {"content-type": request.headers.get("content-type") or "application/octet-stream", **credential}
    try:
        async with http.stream("POST", url, content=body, params={"name": name} if name else None, headers=headers, timeout=settings.serve_timeout) as answer:
            if answer.status_code == 405:
                raise ServeAppHasNoIngressError(f"the Serve app '{app}' has no HTTP ingress: deploy an app that accepts POST")
            if answer.status_code >= 400:
                raise ServeUpstreamError(f"Serve answered {answer.status_code} for the app '{app}'")
            content = bytearray()
            async for chunk in answer.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_INFER_ANSWER_BYTES:
                    raise ServeUpstreamError(f"Serve's answer is larger than {MAX_INFER_ANSWER_BYTES // (1024 * 1024)} MB")
            media_type = answer.headers.get("content-type", "application/octet-stream")
    except httpx.RequestError as exc:
        log.warning("serve unreachable for inference", extra={"app": app, "error": type(exc).__name__})
        raise ServiceUnavailableError(f"Serve is unreachable at {settings.serve_url}") from exc
    return Response(content=bytes(content), media_type=media_type, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
