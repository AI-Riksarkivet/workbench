"""flows-only settings.

`service_kit.config.Settings` is the shared base (api_prefix, CORS, OTel), which `make_service_app` builds and puts on
`app.state.settings`. This class extends it with the fields that are nobody else's business, so it reads every field
from its one environment name, as the base does.

The Serve ORIGIN is the important one. It is `:8000` — Ray Serve's own HTTP ingress — which is a
different port and a different protocol from the Ray dashboard (`:8265`) that `settings.ray_dashboard_url`
names and that `/api/serve/*` proxies read-only. Introspection and invocation are two doors; this is
the invocation one.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from service_kit.config import Settings
from service_kit.governed.settings import GovernedAuthSettings


class FlowsSettings(GovernedAuthSettings, Settings):
    #: Ray Serve's HTTP ingress origin. A `model` node POSTs to `{serve_url}/{app}`.
    serve_url: str = Field(default="http://localhost:8000", alias="WORKBENCH_FLOWS_SERVE_URL")

    #: Per-node Serve call budget. Generous because an HTR page through `/htrflow` is a real
    #: pipeline, not a token stream — the models zone's own inference route allows 180 s.
    serve_timeout: float = Field(default=180.0, alias="WORKBENCH_FLOWS_SERVE_TIMEOUT")

    #: The mounted file holding this service's projected ServiceAccount token for Serve (audience ``serve``), read on
    #: every call (`flows.serve_auth`). Unset, every call to Serve is refused rather than sent without an identity.
    serve_token_file: str | None = Field(default=None, alias="WORKBENCH_FLOWS_SERVE_TOKEN_FILE")

    #: The flows service's own Postgres, which holds each run and each step's outcome (`flows.store`). Unset, the run
    #: doors answer 503 naming it, and the catalog, validation and inference doors still serve. Every value here is a
    #: name, never a credential: the password is a mounted file, read on every new connection so a rotated one takes
    #: effect.
    state_host: str | None = Field(default=None, alias="WORKBENCH_FLOWS_STATE_HOST")
    state_port: int = Field(default=5432, ge=1, le=65535, alias="WORKBENCH_FLOWS_STATE_PORT")
    state_database: str = Field(default="flows", min_length=1, alias="WORKBENCH_FLOWS_STATE_DATABASE")
    state_user: str = Field(default="flows", min_length=1, alias="WORKBENCH_FLOWS_STATE_USER")
    state_password_file: Path | None = Field(default=None, alias="WORKBENCH_FLOWS_STATE_PASSWORD_FILE")
    #: How the server is verified. `verify-full` by default, against `state_ca_file`: flows trusts a database by its
    #: certificate, never by its address. `disable` is for a server on the loopback only (the local stack).
    state_sslmode: Literal["disable", "verify-full"] = Field(default="verify-full", alias="WORKBENCH_FLOWS_STATE_SSLMODE")
    state_ca_file: Path | None = Field(default=None, alias="WORKBENCH_FLOWS_STATE_CA_FILE")
    #: The most connections one process holds.
    state_pool_size: int = Field(default=10, ge=2, le=100, alias="WORKBENCH_FLOWS_STATE_POOL_SIZE")

    #: How long a driver's lease on a run lasts past each renewal. The driver renews it every third of this while it
    #: drives, so it bounds how long a run whose driver died waits before another replica's re-drive resumes it.
    run_lease_seconds: float = Field(default=30.0, ge=1, le=600, alias="WORKBENCH_FLOWS_RUN_LEASE_SECONDS")
    #: How often each replica looks for runs no live driver holds, with jitter of up to a fifth either way.
    run_redrive_seconds: float = Field(default=10.0, gt=0, le=600, alias="WORKBENCH_FLOWS_RUN_REDRIVE_SECONDS")
    #: The most runs one replica drives at once. Each driver takes a pooled connection only for one store call at a
    #: time, and this keeps drivers from crowding the pool requests are served from.
    run_concurrency: int = Field(default=4, ge=1, le=50, alias="WORKBENCH_FLOWS_RUN_CONCURRENCY")
    #: How many times a run may be claimed. A run whose drivers kept stopping before it ended (a process killed in the
    #: same step each time, a database refusing its writes) fails for good past this, with the reason.
    run_max_attempts: int = Field(default=5, ge=1, le=100, alias="WORKBENCH_FLOWS_RUN_MAX_ATTEMPTS")
    #: How long an ended run stays readable, in days; every replica's sweep deletes older ones with their steps. A run
    #: holds at most `MAX_GRAPH_NODES` x `MAX_PAYLOAD_CHARS` of payload (64 MiB), so this bounds the store.
    run_retention_days: float = Field(default=30.0, gt=0, le=3650, alias="WORKBENCH_FLOWS_RUN_RETENTION_DAYS")

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        """A state connection that could not verify its server, or that would cross the network unencrypted, fails here,
        and so do drivers that could take every pooled connection."""
        if self.run_concurrency >= self.state_pool_size:
            raise ValueError(
                f"WORKBENCH_FLOWS_RUN_CONCURRENCY ({self.run_concurrency}) must stay below WORKBENCH_FLOWS_STATE_POOL_SIZE ({self.state_pool_size}), "
                "so run drivers never hold every connection requests are served from"
            )
        if self.state_host is None:
            return self
        if self.state_sslmode == "verify-full" and self.state_ca_file is None:
            raise ValueError(
                "WORKBENCH_FLOWS_STATE_SSLMODE=verify-full needs WORKBENCH_FLOWS_STATE_CA_FILE: the CA the database's certificate is checked against"
            )
        if self.state_sslmode == "disable" and self.state_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(f"WORKBENCH_FLOWS_STATE_SSLMODE=disable is for a database on the loopback; {self.state_host} is not")
        return self


def build_flows_settings() -> FlowsSettings:
    """Read once, at startup, from the process's environment, as `service_kit.build_settings` reads the shared ones."""
    return FlowsSettings.model_validate({})
