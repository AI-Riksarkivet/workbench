"""Per-service settings — the shared media-library config plus the annotator's own knobs.

The annotator is a client of the lakehouse: it reads governed tables through the catalog's doors as the person, on a
token it exchanges for them, and writes annotations and publishes as the workbench's own confidential client, naming
the person in each commit. So its settings name the catalog and that client (`LakehouseTokenSettings`), and never a store
credential: a corpus is opened on the credential the catalog vends.

It mixes in :class:`GovernedAuthSettings` for the person it serves: every entity the annotator keeps (projects, tasks,
claims, drafts) is keyed on who owns or claims it, and what they may do is the workbench's own store's answer.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, Field, model_validator

from client_kit.media.config import Settings
from service_kit.governed.settings import GovernedAuthSettings, LakehouseTokenSettings


class LineageSink(StrEnum):
    """Where a save the annotator commits to a local directory catalog logs its OpenLineage RunEvent.

    Only a directory catalog's save emits: a write through the catalog's REST door is announced by the catalog, so
    emitting here too would count the run twice (`AnnotatorSettings.effective_lineage_sink`).
    """

    log = "log"
    none = "none"


class AssistBackend(BaseModel):
    """One registered assist producer: its endpoint plus its DECLARED contract.

    ``returns``/``inputs`` are what the backend itself claims (shape types out, prompt kinds
    in) — the registry surface, the panel's contract line and task compatibility all derive
    from these declarations rather than from a hardcoded family map in code. Empty means
    undeclared, and every surface must keep saying "unknown" rather than guess.
    A bare URL string is accepted as well: ``{"sam": "http://sam:9000"}``.
    """

    url: str
    returns: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_bare_url(cls, value: object) -> object:
        return {"url": value} if isinstance(value, str) else value


class AnnotatorSettings(GovernedAuthSettings, LakehouseTokenSettings, Settings):
    service_name: str = "annotator"
    service_port: int = Field(default=8103, alias="ANNOTATOR_PORT")

    #: The lakehouse namespace that holds the workbench's annotation tables (`<warehouse>$<namespace>`), which the
    #: workbench's own identity is granted to write: one table per (project, dataset), so a project's annotations are
    #: its own rows and no other project's write can reach them (`annotator.annotations.store`).
    annotations_namespace: str = Field(default="workbench$annotations", min_length=3, alias="ANNOTATOR_ANNOTATIONS_NAMESPACE")

    #: The namespace of a project's warehouse a publish lands in when it names none: `<warehouse>$<this>`. One segment;
    #: the lakehouse gives the name no meaning, so a project's admin creates it, under the run rule if they choose.
    publish_namespace: str = Field(default="published", pattern=r"^[A-Za-z0-9_-]{1,64}$", alias="ANNOTATOR_PUBLISH_NAMESPACE")

    #: Where a local-mode save logs its RunEvent; see `LineageSink`.
    lineage_sink: LineageSink = Field(default=LineageSink.log, alias="ANNOTATOR_LINEAGE_SINK")

    # Interactive AI-assist model endpoint — a Ray Serve deployment (GroundingDINO/SAM). Unset ⇒ a deterministic mock
    # so the draw/prompt→shapes round-trip is testable in-repo (drop-in for the Ray Serve HTTP endpoint).
    assist_url: str | None = Field(default=None, alias="ANNOTATOR_ASSIST_URL")
    # The producer REGISTRY: producer name/prefix → backend, as a JSON object. Routing is longest-prefix, so `"sam": …`
    # covers `sam-click`/`sam-box`; `assist_url` stays the fallback for anything unmapped. An entry is a bare URL string
    # or a STRUCTURED declaration: {"vlm": {"url": "http://vllm:8000", "returns": ["bbox"], "inputs": ["prompt"]}}.
    # `returns`/`inputs` are the backend's DECLARED contract; without them a registered producer renders "returns
    # unknown" and task compatibility cannot compute.
    assist_backends: dict[str, AssistBackend] = Field(default_factory=dict, alias="ANNOTATOR_ASSIST_BACKENDS")
    # Producer DISCOVERY from the Ray Serve control plane: a deployment that carries a `labeling` block in its
    # user_config is registered by being deployed. The discovery URL is the Ray dashboard base (:8265); the proxy URL is
    # the Serve HTTP ingress the discovered route_prefixes hang under (:8000). Under KubeRay set BOTH to the CR's stable
    # Services — discovery at `<rayservice>-head-svc:8265`, proxy at `<rayservice>-serve-svc:8000` — because the
    # serve-svc load-balances across every worker holding Serve replicas, while the derived fallback (dashboard host +
    # Serve's http_options port) reaches only the HEAD's proxy. The registry entries above win on a name conflict.
    serve_discovery_url: str | None = Field(default=None, alias="ANNOTATOR_SERVE_DISCOVERY_URL")
    serve_proxy_url: str | None = Field(default=None, alias="ANNOTATOR_SERVE_PROXY_URL")
    # A batch-labeling job's submit endpoint. Unset ⇒ a deterministic in-repo mock, so the submit/poll round-trip is
    # wired and testable. The annotator only enqueues; the deriver is the caller's own job.
    jobs_url: str | None = Field(default=None, alias="ANNOTATOR_JOBS_URL")

    @property
    def effective_lineage_sink(self) -> LineageSink:
        """The sink a save uses: ``none`` on the catalog's REST door, which announces the write itself; a local directory
        catalog announces nothing, so there the annotator logs its own."""
        if self.catalog_uri:
            return LineageSink.none
        return self.lineage_sink


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
