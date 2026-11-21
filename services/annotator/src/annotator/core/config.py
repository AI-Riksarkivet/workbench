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

from pydantic import Field, model_validator

from annotator.model_endpoint.client import ModelEndpoint
from workbench.media.config import GovernedMediaSettings
from workbench.sources import annotatable


class AnnotatorSettings(GovernedMediaSettings):
    #: The largest request body the annotator reads. An item's whole work is held to 8 MiB (`state.store.MAX_DRAFT_BYTES`)
    #: and an import to this same bound once inflated, so a body twice that leaves room for JSON's own overhead and no
    #: room for a body whose content could never be kept; the media apps' 32 MiB is for uploads the annotator takes none of.
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=0, alias="WORKBENCH_MAX_BODY_BYTES")

    #: The namespace of a project's warehouse a publish lands in when it names none: `<warehouse>$<this>`. One segment;
    #: the lakehouse gives the name no meaning, so a project's admin creates it, under the run rule if they choose.
    publish_namespace: str = Field(default="published", pattern=r"^[A-Za-z0-9_-]{1,64}$", alias="ANNOTATOR_PUBLISH_NAMESPACE")

    #: The model endpoint assist asks, by the workbench's model protocol (`annotator.model_endpoint`, ADR 0023). The
    #: workbench deploys no model: unset, this deployment has no assist, and says so.
    model_endpoint_url: str | None = Field(default=None, pattern=r"^https?://[^\s]+$", alias="ANNOTATOR_MODEL_ENDPOINT_URL")
    #: A credential: the mounted file holding the token the annotator presents to the endpoint as itself (a projected
    #: ServiceAccount token for the endpoint's audience), read on every call. Required with an endpoint.
    model_endpoint_token_file: Path | None = Field(default=None, alias="ANNOTATOR_MODEL_ENDPOINT_TOKEN_FILE")
    #: How long an ask for proposals may take before the person is told the endpoint did not answer.
    model_endpoint_timeout_seconds: float = Field(default=30.0, gt=0, le=600, alias="ANNOTATOR_MODEL_ENDPOINT_TIMEOUT_SECONDS")

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
        lakehouse's, or one of a source git marks annotatable (`workbench.sources.annotatable`)."""
        if self.default_dataset is not None and (reason := annotatable(self.sources.locate(self.default_dataset).source)):
            raise ValueError(f"WORKBENCH_DEFAULT_DATASET {self.default_dataset!r} cannot be the annotator's default: {reason}")
        return self

    @model_validator(mode="after")
    def _an_endpoint_is_called_as_the_annotator(self) -> Self:
        """A model endpoint is never called without an identity: naming one names the token presented to it."""
        if self.model_endpoint_url is not None and self.model_endpoint_token_file is None:
            raise ValueError("ANNOTATOR_MODEL_ENDPOINT_URL needs ANNOTATOR_MODEL_ENDPOINT_TOKEN_FILE: the token the annotator presents to the endpoint")
        return self

    @property
    def model_endpoint(self) -> ModelEndpoint:
        """The deployment's model endpoint as assist calls it."""
        return ModelEndpoint(url=self.model_endpoint_url, token_file=self.model_endpoint_token_file, timeout_seconds=self.model_endpoint_timeout_seconds)

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
