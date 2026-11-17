"""Per-service settings — the shared media-library config plus the annotator's own knobs.

The annotator is a client of the lakehouse: it reads governed tables through the catalog's doors as the person, on a
token it exchanges for them, and publishes a project's accepted work as the workbench's own confidential client, naming
the person in the commit. The work people do before that is its own, in its own Postgres (`state_*`), so it labels
with no lakehouse at all; only a publish needs one. So its settings name the catalog and that client (`LakehouseTokenSettings`), and never a store
credential: a corpus is opened on the credential the catalog vends.

It mixes in :class:`GovernedAuthSettings` for the person it serves: every entity the annotator keeps (projects, tasks,
claims, drafts) is keyed on who owns or claims it, and what they may do is the workbench's own store's answer.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from client_kit.media.config import Settings
from client_kit.sources import annotatable
from service_kit.governed.settings import GovernedAuthSettings, LakehouseTokenSettings


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

    #: The namespace of a project's warehouse a publish lands in when it names none: `<warehouse>$<this>`. One segment;
    #: the lakehouse gives the name no meaning, so a project's admin creates it, under the run rule if they choose.
    publish_namespace: str = Field(default="published", pattern=r"^[A-Za-z0-9_-]{1,64}$", alias="ANNOTATOR_PUBLISH_NAMESPACE")

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

    #: The annotator's own Postgres, which holds its projects, tasks, drafts and claims (`annotator.state`). Unset, the
    #: project and task plane answers 503 naming it, and the read plane still serves. Every value here is a name, never
    #: a credential: the password is a mounted file, read on every new connection so a rotated one takes effect.
    state_host: str | None = Field(default=None, alias="ANNOTATOR_STATE_HOST")
    state_port: int = Field(default=5432, ge=1, le=65535, alias="ANNOTATOR_STATE_PORT")
    state_database: str = Field(default="annotator", min_length=1, alias="ANNOTATOR_STATE_DATABASE")
    state_user: str = Field(default="annotator", min_length=1, alias="ANNOTATOR_STATE_USER")
    state_password_file: Path | None = Field(default=None, alias="ANNOTATOR_STATE_PASSWORD_FILE")
    #: How the server is verified. `verify-full` by default, against `state_ca_file`: the annotator trusts a database
    #: by its certificate, never by its address. `disable` is for a server on the loopback only (the local stack).
    state_sslmode: Literal["disable", "verify-full"] = Field(default="verify-full", alias="ANNOTATOR_STATE_SSLMODE")
    state_ca_file: Path | None = Field(default=None, alias="ANNOTATOR_STATE_CA_FILE")
    #: The most connections one process holds.
    state_pool_size: int = Field(default=10, ge=2, le=100, alias="ANNOTATOR_STATE_POOL_SIZE")

    #: How long a publish run's lease on its project lasts past each renewal. Renewed between the saga's steps, so it is
    #: set to outlast the longest single step, the catalog create (up to `CREATE_ATTEMPTS` calls of pylance's REST client,
    #: whose own request timeout is not measured here). A step that outlasts it anyway lets a second run start beside
    #: the first, which the saga's idempotency by publish token makes safe (`annotator.projects.saga`).
    publish_lease_seconds: int = Field(default=300, ge=30, le=3600, alias="ANNOTATOR_PUBLISH_LEASE_SECONDS")
    #: How often each replica looks for publishes no live run holds, with jitter of up to a fifth either way.
    publish_redrive_seconds: float = Field(default=30.0, gt=0, le=600, alias="ANNOTATOR_PUBLISH_REDRIVE_SECONDS")
    #: The most publish runs one replica drives at once. Each run takes a pooled connection only for one store call at a
    #: time, and this keeps runs from crowding the pool requests are served from.
    publish_concurrency: int = Field(default=2, ge=1, le=20, alias="ANNOTATOR_PUBLISH_CONCURRENCY")

    @model_validator(mode="after")
    def _default_is_annotatable(self) -> Self:
        """A default dataset is what an item naming none is sent from, so it must be one that can be annotated: a
        lakehouse's, or one of a source git marks annotatable (`client_kit.sources.annotatable`)."""
        if self.default_dataset is not None and (reason := annotatable(self.sources.locate(self.default_dataset).source)):
            raise ValueError(f"MEDIA_DEFAULT_DATASET {self.default_dataset!r} cannot be the annotator's default: {reason}")
        return self

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        """A state connection that could not verify its server, or that would cross the network unencrypted, fails here,
        and so do publish runs that could take every pooled connection."""
        if self.publish_concurrency >= self.state_pool_size:
            raise ValueError(
                f"ANNOTATOR_PUBLISH_CONCURRENCY ({self.publish_concurrency}) must stay below ANNOTATOR_STATE_POOL_SIZE ({self.state_pool_size}), "
                "so publish runs never hold every connection requests are served from"
            )
        if self.state_host is None:
            return self
        if self.state_sslmode == "verify-full" and self.state_ca_file is None:
            raise ValueError("ANNOTATOR_STATE_SSLMODE=verify-full needs ANNOTATOR_STATE_CA_FILE: the CA the database's certificate is checked against")
        if self.state_sslmode == "disable" and self.state_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(f"ANNOTATOR_STATE_SSLMODE=disable is for a database on the loopback; {self.state_host} is not")
        return self


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
