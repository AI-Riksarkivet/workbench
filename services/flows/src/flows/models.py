"""The flows wire contract — every model the service accepts, returns, or stores for a run.

One module for all of them, deliberately: a run's job and each step's outcome are stored as these models
(`flows.store`), so a type that lives elsewhere is a type whose stored shape nobody owns. Pydantic v2 throughout (no
dataclasses) — the frontend parses these bodies, and the catalog response shape in particular is PINNED (see
`catalog.py`).
"""

import hashlib
import json
import math
from http import HTTPStatus
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from service_kit.exceptions import DomainError


#: A node's `output_text` is truncated to this many characters before it leaves the service.
#: A run is a sandbox probe, not a transport: a model node can return a whole page of ALTO, and
#: three of those in one RunState is a response nobody reads. The frontend re-fetches the full payload from its own
#: inference call when it needs it.
MAX_OUTPUT_CHARS = 4000

#: The ceiling on `NodeResult.payload_text` — the value the GRAPH carries downstream, two orders of
#: magnitude above the display cap because it is a document, not a preview. It is stored once per step
#: (`run_steps.payload`) and read back as the input of every dependent step, so it bounds what one step writes.
#:
#: 256 KiB exactly matches `executor._REGEX_MAX_SUBJECT`, the other caller-facing bound on a payload
#: in this service, so a payload that survives this cap is still one a `regex` node will accept —
#: two different ceilings would make "too large" mean two different things one edge apart.
MAX_PAYLOAD_CHARS = 256 * 1024

#: Appended to a payload this service cut, so a downstream node's input SAYS it is incomplete rather
#: than reading as a short document. Formatted with the original length and the ceiling: those two
#: numbers are what turn "the answer looks wrong" into "the payload was 3 MiB".
PAYLOAD_TRUNCATED_MARKER = "\n…[flows truncated this payload: {total} characters exceeded the {cap}-character ceiling]"

#: The two STRUCTURAL bounds. `MAX_PAYLOAD_CHARS` caps one payload; these cap how many of them a
#: single run can hold. Without them the payload cap bounded nothing in aggregate — the ceiling on a run was the ceiling
#: on a drawing.
#:
#: `MAX_NODE_FAN_IN` is the sharp one, and it is reachable with a two-node graph. The driver builds each step's input as
#: one payload per incoming EDGE (`graph.upstreams`), so ONE `NodeJob` carries one full payload per edge: 8 x 256 KiB =
#: 2 MiB at most. Counting edges rather than distinct sources is deliberate: `validate_graph` tolerates duplicate edges
#: on purpose (a drag-created duplicate is easy to produce on a canvas), so one upstream dragged N times puts N payloads
#: in one step's input while presenting a single source.
#:
#: Both numbers are DEFAULTS, not from a profile of real flows — a visual builder graph does not approach either.
MAX_GRAPH_NODES = 256
MAX_NODE_FAN_IN = 8

#: A run's vocabulary. A run that someone stopped is `cancelled`: its recorded steps keep their outcomes, and the steps
#: it never reached have none.
RunStatus = Literal["running", "succeeded", "failed", "cancelled"]

#: A step's outcome. A node that never got to run because an upstream failed is `failed` with the error
#: "upstream failed" — not a third state, because "blocked" is a *reason*, not an outcome.
NodeStatus = Literal["succeeded", "failed"]

#: The longest id a node, an edge end or a handle may have. A node id is half of a step's key (`run_steps`' primary
#: key, whose btree row Postgres caps at 2704 bytes) and of the step's Idempotency-Key; 128 characters (at most 512
#: bytes of UTF-8) stays far under both and above any id a canvas mints.
MAX_ID_CHARS = 128
#: The longest node kind: a name from the catalog.
MAX_KIND_CHARS = 64

type NodeId = Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)]


class FlowNode(BaseModel):
    """One node of the drawn graph. `kind` selects the dispatch; `config` is that kind's params."""

    model_config = ConfigDict(extra="ignore")

    id: NodeId
    kind: str = Field(max_length=MAX_KIND_CHARS)
    config: dict[str, object] = Field(default_factory=dict)


class FlowEdge(BaseModel):
    """A directed payload edge. The handle ids are carried but not interpreted in v0 —
    dispatch is per-kind and takes all upstream payloads in edge order."""

    model_config = ConfigDict(extra="ignore")

    source: NodeId
    target: NodeId
    source_handle: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    target_handle: str | None = Field(default=None, max_length=MAX_ID_CHARS)


class FlowGraph(BaseModel):
    #: `forbid`, unlike the node/edge models above — and for a measured reason. Both fields default
    #: to empty, so under `ignore` ANY wrong-shaped body parsed as a valid empty graph and
    #: `/validate` answered `{"ok": true, "problems": []}` — a false all-clear, the worst possible
    #: reply from a validator. The mistake is not hypothetical: `/validate` takes a bare graph while
    #: `/runs` takes `{graph, seeds}`, so posting the wrapped shape to `/validate` is the obvious
    #: slip, and it is exactly what happened the first time this endpoint was driven by hand. Now it
    #: is a 422 naming the unexpected key. The nodes and edges keep `ignore` deliberately: a drawn
    #: node carries editor-only fields (position, label) that the server has no business rejecting.
    model_config = ConfigDict(extra="forbid")

    # BOUNDED HERE, not only in `validate_graph`. `graph.py:41` already refuses an over-ceiling graph
    # — but it does so AFTER pydantic has constructed every node, so it bounded the ANSWER and not
    # the WORK. Measured: 500,000 nodes = 23.26 MiB = 3.00 s of event-loop block before replying
    # "over the 256-node ceiling". Flows runs one replica and one loop, so that also stalls every
    # other flows request and `/livez` — a caller who never gets past validation could take the
    # service out. `max_length` moves the refusal to the boundary, where the work stops.
    #
    # The ceiling in `graph.py` STAYS: it reports the problem in the caller's own vocabulary
    # alongside the other hygiene problems, which a 422 cannot do. This is the cheap guard in front
    # of it, not a replacement.
    nodes: list[FlowNode] = Field(default_factory=list, max_length=MAX_GRAPH_NODES)
    # Edges are bounded by the fan-in rule per node, so the graph-wide ceiling is nodes x fan-in.
    edges: list[FlowEdge] = Field(default_factory=list, max_length=MAX_GRAPH_NODES * MAX_NODE_FAN_IN)


class ParamSpec(BaseModel):
    """One configurable field of a node kind, as the palette should render it."""

    name: str
    label: str
    type: Literal["string", "number", "boolean", "select"]
    required: bool = False
    #: Only meaningful for `type == "select"`. Empty means "the client supplies the options" —
    #: which is the case for a model node's `app`, whose options are the LIVE Serve deployments
    #: read from `/api/serve/applications/`. The server cannot enumerate them without reaching
    #: the cluster, and a catalog that needed the cluster to answer would be useless offline.
    options: list[str] = Field(default_factory=list)


class NodeSpec(BaseModel):
    """A node kind as the server declares it (graphbook's model: the catalog is the seam that
    lets a server-side node kind appear in the palette without a frontend release)."""

    kind: str
    label: str
    role: Literal["source", "transform", "sink"]
    params: list[ParamSpec] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    """PINNED SHAPE — the frontend parses this: `{"version": 1, "kinds": [NodeSpec, ...]}`.

    `version` is an integer the client can branch on, not the service version. Bump it only when
    the *shape* changes incompatibly; adding a kind or a param is not a bump.
    """

    version: Literal[1] = 1
    kinds: list[NodeSpec]


class ValidateResponse(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)


#: The RFC 9457 `type` a refused graph answers. One constant, spent by the exception that RAISES the
#: refusal and by the model that DOCUMENTS it, so the wire and the OpenAPI cannot drift apart.
FLOW_INVALID_TYPE = "about:blank#flow-invalid"
#: The title a refused run answers with, in the OpenAPI model and on the wire alike.
REFUSED_TITLE = "Unprocessable Entity"


class RunRefused(BaseModel):
    """The 422 body for a run whose graph does not validate — RFC 9457 problem+json plus the
    structured `problems` list (an extension member, which the RFC explicitly allows).

    DOCUMENTATION ONLY: this is what `create_run` declares under `responses={422: ...}` so the
    OpenAPI describes the refusal. The body itself is produced by the estate's one problem handler
    from `RunRefusedError` below — the route used to build this model and return it as a
    `JSONResponse`, which made this service the only one with a second error plane
    (FLOWS-422-BYPASSES-HIERARCHY).
    """

    type: str = FLOW_INVALID_TYPE
    title: str = REFUSED_TITLE
    status: int = 422
    detail: str
    problems: list[str] = Field(default_factory=list)


class RunRefusedError(DomainError):
    """A graph that does not validate, RAISED rather than returned.

    The reason this could not exist before is recorded in `service_kit.exceptions`: `_problem` built
    four keys and dropped everything else, so the `problems` LIST — the whole substance of this
    refusal, and what the studio builder highlights nodes from — had nowhere to ride. The hierarchy
    now carries extension members, so the refusal is an exception like every other refusal in the
    estate, and `create_run` declares one return type again.

    Raise it as `RunRefusedError(f"{len(problems)} problem(s) in the graph", extensions={"problems":
    problems})`.
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = REFUSED_TITLE
    problem_type = FLOW_INVALID_TYPE


def unstorable(text: str) -> str | None:
    """Why the run store cannot keep ``text``, or None when it can.

    Postgres keeps no NUL character in `text` or `jsonb` (both refuse `\\u0000`), and no string that is not UTF-8 (a
    lone surrogate, which JSON's `\\ud800` escape decodes to). A run carries caller text into both, so what it cannot
    keep is refused at the door and turned into a step's failure when a step produces it.
    """
    if "\x00" in text:
        return "a NUL character (\\u0000), which the run store cannot keep"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "a lone surrogate, which is not UTF-8 and which the run store cannot keep"
    return None


#: How deep a node's config may nest. A drawn node's config is a few flat fields; the bound keeps every walk of it (this
#: module's, the digest's, the store's) far from Python's recursion limit.
MAX_CONFIG_DEPTH = 32


def _refuse_unkeepable(value: object) -> None:
    """Refuse a parsed JSON value the run store cannot keep, walked without recursion: text holding what `unstorable`
    names, a number that is not finite (jsonb has no NaN or infinity, and JSON `1e400` parses to one), or nesting past
    `MAX_CONFIG_DEPTH`."""
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_CONFIG_DEPTH:
            raise ValueError(f"the run nests deeper than {MAX_CONFIG_DEPTH} levels in a node's config")
        if isinstance(item, str):
            if reason := unstorable(item):
                raise ValueError(f"the run holds {reason}: remove it from the seeds and the node config")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"the run holds a number that is not finite ({item}), which the run store cannot keep")
        elif isinstance(item, dict):
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            pending.extend((part, depth + 1) for part in item)


class RunRequest(BaseModel):
    """A run: the graph, plus the text payload for each source node.

    `seeds` is keyed by NODE ID and holds text only. Bytes inputs (the `image` node kind) are a
    FRONTEND-ONLY lane in v0: an image rides the studio zone's own `+server.ts` straight to Serve
    because base64-ing it through a JSON envelope costs a third more bytes and triple-buffers it
    (the estate's transport ruling). So a server-side run that reaches an `image` node fails that
    node by design — see `executor.dispatch` — rather than pretending to have data it never got.
    """

    graph: FlowGraph
    seeds: dict[NodeId, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _storable(self) -> Self:
        """Refuse, as a 422, a request holding a value the run store cannot keep: it would fail the door at the write."""
        _refuse_unkeepable(self.seeds)
        for node in self.graph.nodes:
            _refuse_unkeepable([node.id, node.kind, node.config])
        for edge in self.graph.edges:
            _refuse_unkeepable([edge.source, edge.target, edge.source_handle, edge.target_handle])
        return self

    def digest(self) -> str:
        """The request's canonical digest: SHA-256 of its graph and seeds as JSON with sorted keys. A replay of an
        Idempotency-Key is judged by it, computed the same way from each request as sent, so no store round trip of the
        stored copy changes the answer."""
        canonical = RunRequest(graph=self.graph, seeds=self.seeds).model_dump(mode="json")
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class RunJob(RunRequest):
    """What a run executes, stored whole on its row (`runs.job`): a run request plus the resolved Serve origin.

    Separate from `RunRequest` because the client does not get to choose which cluster a run talks
    to — the service resolves that from its own settings when the run starts and the row carries
    the resolved value, for the same reason `NodeJob` does: a run may be re-driven by any replica,
    and re-reading the origin from that replica's environment is how one run ends up half-executed
    against two clusters.
    """

    serve_url: str
    serve_timeout: float = 180.0
    #: Where the service's Serve credential is mounted; the path rides the job, the token is read at each call.
    serve_token_file: str | None = None
    #: The project the run belongs to: its roles decide who may read and stop it.
    project: str


class NodeRunState(BaseModel):
    """The outcome of one node: what the builder paints on it (status colour, timing, output)."""

    status: NodeStatus
    #: Wall time for THIS node only, milliseconds. None when the node never executed.
    ms: float | None = None
    output_text: str | None = None
    error: str | None = None

    @field_validator("output_text")
    @classmethod
    def _truncate(cls, value: str | None) -> str | None:
        # Enforced on the MODEL, not at each construction site: the executor and the driver both build these, and a
        # stored outcome is validated again when it is read back.
        if value is not None and len(value) > MAX_OUTPUT_CHARS:
            return value[:MAX_OUTPUT_CHARS]
        return value


class RunState(BaseModel):
    """A whole run, addressable at `GET /flows/projects/{project}/runs/{run_id}`."""

    run_id: str
    #: The project the run belongs to.
    project: str = ""
    status: RunStatus
    nodes: dict[str, NodeRunState] = Field(default_factory=dict)
    error: str | None = None


class NodeResult(BaseModel):
    """What one node's execution produced, stored as the step's outcome (`run_steps`).

    Two fields because they answer two questions, and the two carry DIFFERENT ceilings. `state` is
    what the builder paints, capped at ``MAX_OUTPUT_CHARS`` (4 000) because it is a preview.
    `payload_text` is what the GRAPH carries to the dependent nodes, capped at ``MAX_PAYLOAD_CHARS``
    (256 KiB) because it is a document — a page of ALTO exceeds the display cap routinely, and
    feeding the truncated PREVIEW downstream would hand an `alto` node a document cut mid-element.

    The graph cap is loud, which is the whole reason it can exist at all: a truncated payload ends
    with ``PAYLOAD_TRUNCATED_MARKER`` naming the original length, so a document cut mid-element
    announces itself in the dependent node's own input instead of producing a plausible short
    answer. A silent truncation here would be worse than no cap; a marked one is not.
    """

    state: NodeRunState
    payload_text: str | None = None

    @field_validator("payload_text")
    @classmethod
    def _cap(cls, value: str | None) -> str | None:
        # On the MODEL for the same reason `NodeRunState._truncate` is: this is the exact value stored as the step's
        # payload and read back as every dependent's input, and the cut is idempotent, so a payload read back and
        # validated again is unchanged.
        if value is None or len(value) <= MAX_PAYLOAD_CHARS:
            return value
        marker = PAYLOAD_TRUNCATED_MARKER.format(total=len(value), cap=MAX_PAYLOAD_CHARS)
        # The marker rides INSIDE the ceiling, not past it: the cap is what a dependent node
        # receives, and a bound that its own input check (`_REGEX_MAX_SUBJECT`) then rejects by a
        # few dozen characters would be a bound that fails at exactly the size it was set for.
        return value[: MAX_PAYLOAD_CHARS - len(marker)] + marker


class Payload(BaseModel):
    """What travels along an edge.

    `kind` exists so the union the frontend already models (`{kind:'bytes'|'text'}`) has a
    server-side counterpart, but v0 only ever produces `text`: the one binary source is the
    `image` node, which is refused server-side (see `RunRequest`). Frozen — a payload is a value.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "bytes"] = "text"
    text: str = ""

    def as_bytes(self) -> bytes:
        return self.text.encode("utf-8")


class NodeJob(BaseModel):
    """One node's execution request.

    Self-contained on purpose. A step may run in any replica, after any re-drive, so it carries
    the resolved Serve origin rather than re-reading it from that replica's environment: two
    derivations of one address is how a run ends up split across two clusters.
    """

    node: FlowNode
    #: Upstream payload texts, in edge order.
    inputs: list[str] = Field(default_factory=list)
    seed: str | None = None
    serve_url: str
    #: Per-call budget in seconds, carried for the same reason as the origin.
    serve_timeout: float = 180.0
    #: Where the service's Serve credential is mounted, carried for the same reason; the token is read at the call.
    serve_token_file: str | None = None


class IdempotencyKeyReusedError(DomainError):
    """An Idempotency-Key that already names a run, sent with a different request."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unprocessable Content"
    problem_type = "about:blank#idempotency-key-reused"


class InferenceTooLargeError(DomainError):
    """An inference body over the door's ceiling, by its declared length or by what it carried."""

    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    title = "Payload Too Large"


class ServeAppHasNoIngressError(DomainError):
    """Serve answered 405: the app at that route has no HTTP handler, so a POST can never reach it."""

    status_code = HTTPStatus.NOT_IMPLEMENTED
    title = "Not Implemented"


class ServeUpstreamError(DomainError):
    """Serve answered the inference with an error of its own."""

    status_code = HTTPStatus.BAD_GATEWAY
    title = "Bad Gateway"
