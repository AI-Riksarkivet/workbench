"""The flows HTTP surface: `/flows/catalog`, and per project `/flows/projects/{project}/validate` and `.../runs[/{run_id}]`.

Mounted under `settings.api_prefix` by `make_service_app`, so the deployed paths are
`/api/flows/...` (the chart sets `WORKBENCH_API_PREFIX=/api`, and `scripts/dev-micro.sh` matches it) —
which is exactly what the cluster route's `/api/flows` rule forwards unrewritten. A router prefix of
`/flows` with an api_prefix of `/api/v1` would serve `/api/v1/flows/...` and every call through the
route would 404; that trap has already been paid for once in this estate (the ingest row's `/v1`).

Every project route is gated on that project's roles in the workbench's own store (`flows.security`), and a run id is
DERIVED from the caller's `Idempotency-Key` rather than minted per attempt — the run id is also the
durable workflow's instance id, so deriving it is what makes a retry converge on the run it is
retrying instead of forking a second one. See `create_run` and `lifespan.DaprFlowScheduler`.
"""

import asyncio
import logging
import uuid
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Header, Path, Query, Request, Response
from pydantic import BaseModel, ValidationError

from flows import executor, security
from flows.audit_actions import FlowsAuditAction
from flows.catalog import CATALOG
from flows.dependencies import FlowRunReader, FlowsSettingsDep, HttpDep, RunReaderDep, RunsDep, SchedulerDep, ScheduleUnconfirmed
from flows.graph import validate_graph
from flows.metrics import DURABLE, INLINE, record_run
from flows.models import (
    CatalogResponse,
    FlowGraph,
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
from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from service_kit.exceptions import ValidationError as BadRequestError


log = logging.getLogger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])

#: A project id, as the annotator mints and names them.
ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: The namespace that makes a run id deterministic across processes, replicas and restarts. A fixed
#: constant, not uuid1/uuid4: the whole point is that the same `Idempotency-Key` yields the same run
#: id — and therefore the same workflow INSTANCE id — on a different pod, after a crash, on a retry.
#: Distinct from `ingest.runs.RUN_NAMESPACE` so the two services cannot collide on one key.
RUN_NAMESPACE = uuid.UUID("2c9b7f41-6d4a-5e18-9a3c-71b0c5d2e480")


def run_id_for(project: str, subject: str, idempotency_key: str) -> str:
    """Derive the run id from the CALLER's key — the estate's idempotency pattern (`ingest.runs`).

    Deterministic by construction: same subject + same key -> same id, on any pod, after any crash.
    A token minted per attempt leaves one orphan run per retry, and it is what made a schedule
    timeout unrecoverable: with nothing to converge on, the only choices were "run it again" and
    "lose it".

    Scoped by PROJECT and SUBJECT, because the run id IS the workflow instance id and it is also the URL of a
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

    GATED since 2026-08-26 (owner ruling: the estate is authenticated). The graph is also bounded at
    the MODEL now — `FlowGraph.nodes` carries `max_length` — because the ceiling in `validate_graph`
    fires only after every node has been built, which bounded the answer and not the work.
    """
    await security.require(checker, subject, security.READ, project, FlowsAuditAction.FLOWS_READ)
    problems = validate_graph(graph)
    return ValidateResponse(ok=not problems, problems=problems)


@router.post(
    "/projects/{project}/runs",
    # NO `response_model=`: the return annotation is `RunState` and FastAPI derives the schema from
    # it, so restating it would carry no information — the argument is reserved for a wire shape that
    # DIFFERS from the annotation. Ruff's FAST001 refuses the restatement here, because this module
    # does not postpone its annotations.
    #
    # HTTPStatus, not `fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY`: starlette deprecated that alias
    # in favour of ..._CONTENT and emits a warning at IMPORT of this module. `service_kit.exceptions`
    # already builds its problem bodies from `http.HTTPStatus`, so this is the estate's spelling too.
    responses={HTTPStatus.UNPROCESSABLE_ENTITY: {"model": RunRefused, "description": "The graph does not validate."}},
)
async def create_run(
    project: ProjectId,
    request: RunRequest,
    http: HttpDep,
    settings: FlowsSettingsDep,
    runs: RunsDep,
    scheduler: SchedulerDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RunState:
    """Execute a flow.

    Validation first, always: an invalid graph is refused with the problem LIST rather than
    half-executed. Then one of two lanes, decided at startup and not by the caller — the durable
    Dapr workflow if a sidecar was found, otherwise inline.

    `Idempotency-Key` makes a RETRY safe: the run id is DERIVED from it (`run_id_for`), so a request
    made after the first one answered resolves to the same resource and the same workflow instance
    rather than to a second run. Two keyed POSTs in flight *simultaneously* converge only in the
    durable lane, where the engine refuses the duplicate instance id — see the dedupe below for the
    exact boundary. A key-less POST still gets a fresh run: without a caller key there is nothing to
    converge on, and inventing one would make every retry a new run while pretending otherwise
    (`ingest.api`).

    The refusal is RAISED, through the estate's one exception hierarchy, and carries the problem list
    as an RFC 9457 extension member. It used to be a hand-built `JSONResponse` — with
    `-> RunState | JSONResponse` on the signature — because `service_kit.exceptions._problem`
    flattened a problem to a single `detail` string and the builder needs the LIST to highlight
    nodes. That reason is gone (`DomainError(..., extensions=...)`), and with it the only route in
    this service whose refusal no handler produced.
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

    existing = runs.get(run_id)
    if existing is not None:
        # The dedupe a RETRY sees: once a run has been recorded, the same key + subject resolves to
        # that resource and starts nothing — the property that makes the 503 below retryable rather
        # than a coin flip. It keys off the RECORD, and `_remember` writes that record only after the
        # lane finishes, so what it covers is a retry made after the first attempt answered. Two
        # keyed POSTs in flight at once both fall through here, and what separates them is the lane,
        # not this line: the durable one dedupes at the DISPATCH (the derived id is the workflow
        # instance id, so the engine refuses the second create as a duplicate and
        # `DaprFlowScheduler` reports the run as durable), while the inline one has no dispatch to
        # dedupe on and runs the graph twice. That is the honest cost of a process-local v0 store
        # (`dependencies.get_runs`) and it is paid only where there is no sidecar; `ingest.api`'s
        # header states the general rule — dedupe keys off the dispatch, not off a record's
        # existence.
        return existing

    state: RunState | None = None
    if scheduler is not None:
        job = RunJob(
            graph=request.graph,
            seeds=request.seeds,
            serve_url=settings.serve_url,
            serve_timeout=settings.serve_timeout,
            serve_token_file=settings.serve_token_file,
            project=project,
        )
        try:
            await scheduler.schedule(run_id, job.model_dump())
        except ScheduleUnconfirmed as exc:
            # NOT the degrade path. The scheduler could not establish whether the instance exists, and
            # `asyncio.wait_for` cancels the wait rather than the gRPC call — so degrading here is how
            # one run executes twice, once durably and once inline, with the caller told about the
            # inline one. 503 is the honest answer and the retryable one: the run id is derived from
            # the caller's key, so retrying with that same key converges on THIS run.
            log.warning("schedule for %s was not confirmed — refusing rather than running it twice", run_id)
            # The scheduler's own sentence carries the bound it actually waited, so the number in the
            # message cannot drift from the number in the code.
            raise ServiceUnavailableError(f"{exc} — retry with the same Idempotency-Key") from exc
        except Exception:
            # DEGRADE to the inline lane rather than 500 — reached only when the scheduler has
            # established that no instance was created (it probes the engine before raising this).
            # A sandbox run must not fail because the DURABLE lane is unavailable: the graph is
            # perfectly runnable here and now, and a 500 tells the user nothing they can act on.
            # Measured live 2026-08-06: a sidecar was injected (so the runtime started and this branch
            # was taken) while `lance-statestore` was not SCOPED to app-id `flows`, so
            # `create_workflow_instance` raised "the state store is not configured to use the actor
            # runtime" — a component the estate does declare with `actorStateStore: "true"`, just not
            # visibly to this app. Logged at exception level because a silently-inline run is the
            # thing an operator must be able to find; the caller still gets a completed run.
            log.exception("durable lane unavailable for %s — falling back to the inline executor", run_id)
        else:
            # `running`, with no node states yet: the engine owns the run from here. Reading the live
            # per-node state back out of the workflow history is the follow-up (open_studio_flows.md
            # defers streaming per-node progress) — v0 proves the seam, it does not stream it.
            state = RunState(run_id=run_id, project=project, status="running")
            record_run(DURABLE)

    if state is None:
        # The lane is decided here, per request — the startup log announces only what the process COULD
        # do, not what this run did.
        record_run(INLINE)
        state = await executor.execute(
            request.graph,
            request.seeds,
            run_id,
            client=http,
            serve_url=settings.serve_url,
            serve_timeout=settings.serve_timeout,
            serve_token_file=settings.serve_token_file,
        )
        state = state.model_copy(update={"project": project})

    _remember(runs, state, settings.max_runs)
    return state


class TerminateAccepted(BaseModel):
    """202 for a request the ENGINE accepted, not a completed stop.

    The SDK's real semantics, stated in the body rather than implied by the status: further
    scheduling stops, but an activity already in flight runs to completion. A caller told "terminated"
    would reasonably assume the Serve replicas were free, and they are not — the same wording
    `ingest.api.TerminateAccepted` uses, for the same reason.
    """

    run_id: str
    detail: str = "further scheduling stops; work already in flight may still complete"


@router.post("/projects/{project}/runs/{run_id}/terminate", status_code=HTTPStatus.ACCEPTED)
async def terminate_run(
    project: ProjectId,
    run_id: str,
    runs: RunsDep,
    reader: RunReaderDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> TerminateAccepted:
    """Stop a durable run (DWF-MGT-003).

    Without this a run could be started and read but never stopped. `flow_run_workflow` fans out
    `run_node` activities against LIVE Ray Serve endpoints, and a wide graph aimed at a wedged Serve
    deployment occupies replicas for `serve_timeout` x NODE_RETRY per node per wave with no way to
    intervene — a pod restart does not help, because the instance is durable and resumes.

    THE SAME relation as start: whoever may spend compute on a project's graph may stop spending it, so the person
    who started a runaway can stop it. AUTHZ BEFORE EXISTENCE, matching `get_run`: no run-id oracle for an unpermitted
    caller, and a run of another project is a 404 here.
    """
    await security.require(checker, subject, security.RUN, project, FlowsAuditAction.FLOWS_RUN_TERMINATE)
    if reader is None:
        raise ServiceUnavailableError("the workflow engine is not reachable from this pod")
    await _run_of(project, run_id, runs, reader)
    # Through a thread: `DaprWorkflowClient` is synchronous gRPC, and blocking the loop here would
    # stall every other request on the worker — the same rule `get_run` follows for `state`.
    await asyncio.to_thread(reader.terminate, run_id)
    return TerminateAccepted(run_id=run_id)


@router.get("/projects/{project}/runs/{run_id}")
async def get_run(
    project: ProjectId,
    run_id: str,
    runs: RunsDep,
    reader: RunReaderDep,
    subject: security.CurrentSubject,
    checker: security.CheckerDep,
) -> RunState:
    """Read a run back.

    A run document carries what the model nodes returned — transcribed page text, prompt output, upstream error
    strings — so it takes the project's read role (a viewer reads a flow's runs). AUTHZ BEFORE EXISTENCE: a 404 that
    only a permitted caller can distinguish from a 403 is one less oracle for guessing run ids, and a run of another
    project is a 404 here.
    """
    await security.require(checker, subject, security.READ, project, FlowsAuditAction.FLOWS_RUN_READ)
    return await _run_of(project, run_id, runs, reader)


async def _run_of(project: str, run_id: str, runs: dict[str, RunState], reader: FlowRunReader | None) -> RunState:
    """The run ``run_id`` of ``project``, or a 404: one of another project is not this project's to show.

    The ENGINE first, the local dict second. In the durable lane the dict only ever holds `RunState(status="running")`
    with an empty `nodes` map — written once by `_remember` and never updated — so preferring it would report a
    completed run as running forever, hide a FAILED run's error, and 404 every run older than `max_runs` or predating a
    restart, all while the workflow itself was fine. The workflow RETURNS `RunState.model_dump()`, so the engine holds
    the real answer and the dict is the cache its own docstring claims to be.
    """
    engine = await asyncio.to_thread(reader.state, run_id) if reader is not None else None
    state = _state_from_engine(run_id, engine) or runs.get(run_id)
    if state is None or state.project != project:
        raise NotFoundError(f"no such run: {run_id}")
    return state


def _state_from_engine(run_id: str, engine: dict[str, object] | None) -> RunState | None:
    """Rebuild a `RunState` from the workflow instance, or None when the engine cannot answer.

    Returning None rather than a placeholder is the point: "the engine has nothing" and "the engine
    says it is running" are different facts, and only the first may fall back to the local record.
    """
    if not engine:
        return None
    status = str(engine.get("runtime_status") or "")
    # The run's project, from the job the workflow was started with: a run the engine holds belongs to the project its
    # input names, whatever state it is in.
    project = _project_of(engine.get("serialized_input"))

    # COMPLETED means the workflow RETURNED — and what it returned is a whole RunState, node states
    # included. Parse it rather than synthesising one: the workflow already decided succeeded-vs-failed
    # by inspecting its own node results, and re-deriving that here would be a second opinion that can
    # disagree with the run's own record of itself.
    if status == "COMPLETED":
        output = engine.get("serialized_output")
        if isinstance(output, str) and output:
            try:
                return RunState.model_validate_json(output)
            except ValidationError:
                # A completed instance whose output will not parse is a contract break between this
                # reader and the workflow, not a missing run. Say so, rather than reporting the run as
                # absent and sending an operator to look for a run that plainly exists.
                log.exception("run %s completed but its output does not parse as a RunState", run_id)
                return RunState(run_id=run_id, project=project, status="failed", error="the run completed but its result could not be read")
        return RunState(run_id=run_id, project=project, status="failed", error="the run completed but returned no result")

    # FAILED/TERMINATED never produced a RunState — the body raised or was killed — so this is the one
    # place the reader must compose the answer itself.
    if status in ("FAILED", "TERMINATED"):
        detail = engine.get("failure_details") or engine.get("_failure_detail")
        return RunState(run_id=run_id, project=project, status="failed", error=str(detail) if detail else f"the workflow engine reports the run as {status}")

    # RUNNING / PENDING / SUSPENDED — genuinely still going. Per-node progress is not streamed yet
    # (open_studio_flows.md defers it), so the honest answer is the status without invented node
    # states; the local record, if any, carries no more than this either.
    if status:
        return RunState(run_id=run_id, project=project, status="running")
    return None


def _project_of(serialized_input: object) -> str:
    """The project a workflow's input `RunJob` names, or empty when it cannot be read (which no project matches)."""
    if not isinstance(serialized_input, str) or not serialized_input:
        return ""
    try:
        return RunJob.model_validate_json(serialized_input).project
    except ValidationError:
        return ""


def _remember(runs: dict[str, RunState], state: RunState, max_runs: int) -> None:
    """Store the run, evicting the oldest first. A dict preserves insertion order, so "oldest" is
    the first key — no timestamps, and therefore nothing that can disagree with the run's own."""
    runs[state.run_id] = state
    while len(runs) > max_runs:
        runs.pop(next(iter(runs)))


#: The most bytes one inference carries: a page scan is a few MB.
MAX_INFER_BYTES = 25 * 1024 * 1024
#: The most bytes of Serve's answer this door relays; an ALTO page or a chat completion is far below it.
MAX_INFER_ANSWER_BYTES = 25 * 1024 * 1024

#: A path within a Serve app: slash-separated segments of URL-safe characters, so the door reaches only the app it
#: names. An OpenAI-compatible app answers under `/v1/...`, which a bare app route cannot reach.
SERVE_PATH = r"^(/[A-Za-z0-9._~-]+)*/?$"

ServeApp = Annotated[str, Query(pattern=executor.APP_PATTERN.pattern, max_length=63, description="The Serve app's route prefix.")]
ServePath = Annotated[str, Query(pattern=SERVE_PATH, max_length=512, description="The path within the app.")]
PageName = Annotated[str, Query(max_length=256, description="The page key Serve stamps into what it answers.")]


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
