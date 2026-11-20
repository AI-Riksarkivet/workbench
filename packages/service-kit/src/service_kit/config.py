"""Settings loaded from environment + .env via pydantic-settings.

Read once at startup via `Settings()`; pass through DI as `SettingsDep`. Never
re-read env vars in routes or services.

`Settings` is what `make_service_app` reads to build any service: its doors' prefix, CORS, docs, the body cap, the
audit trail and telemetry. Every service's own settings class extends it, so the factory takes one shape. No service
holds an object-store endpoint or credential: data is read through lance-namespace, on the storage options the catalog
vends.
"""

import json
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _strings(value: object) -> object:
    """A list of strings given as a JSON list, or as a comma-separated string (`https://a,https://b`)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        return json.loads(text)
    return [part.strip() for part in text.split(",") if part.strip()]


#: A setting holding a list of strings. `NoDecode`, because the environment source JSON-decodes a list before any
#: validator runs, so a plain `https://a` would stop the service at start; the raw string reaches `_strings` instead.
type StringList = Annotated[list[str], NoDecode, BeforeValidator(_strings)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The environment alone, as the chart and the local stack set it: no `.env` file is read.
        # A keyword or a key the class does not name is refused (`extra="forbid"`), so a value given by field name, or
        # misspelt, fails loudly instead of vanishing; an environment variable no field names is never collected, so
        # the process's other variables are left alone (`test_every_setting_has_one_name.py`).
        extra="forbid",
        case_sensitive=False,
        # EACH FIELD IS READ FROM ONE ENVIRONMENT NAME, ITS ALIAS. Validating by field name would also teach the
        # environment source the field's own name (with `env_prefix` before it), a second name for every setting that
        # nothing documents. So settings are given by their aliases alone, in the environment and in
        # `model_validate` alike. Every settings class extends this one and inherits it
        # (`packages/service-kit/tests/test_every_setting_has_one_name.py`).
        validate_by_name=False,
        validate_by_alias=True,
        env_prefix="",
    )

    api_prefix: str = Field(default="/api/v1", alias="WORKBENCH_API_PREFIX")
    #: The origins besides the app's own a browser may call from: a JSON list or a comma-separated string; none by default.
    cors_origins: StringList = Field(default_factory=list, alias="WORKBENCH_CORS_ORIGINS")

    #: Whether this app serves `/docs`, `/redoc` and `/openapi.json`. OFF by default, and the default
    #: is the whole point.
    #:
    #: Four services already carried a `docs_enabled` flag before this one existed — and it defaulted
    #: to True, with no deployment path setting it either way (`grep -rn DOCS chart/ .docker/ scripts/`
    #: matched nothing). A flag nobody sets IS the default, so all four shipped their schemas openly
    #: while looking configurable. A security default that every deployment must remember to turn off
    #: is one nobody turns off.
    #:
    #: What this closes is a route table, parameter names and request/response schemas — not data, and
    #: every documented route stays individually auth-gated.
    docs_enabled: bool = Field(default=False, alias="WORKBENCH_DOCS")

    #: Whether this app emits the `workbench.audit` compliance stream. ON by default, because the trail
    #: it gates is evidence: a deployment that wants none says so, and one that forgets still has it.
    #:
    #: THE FLAG EXISTS SO THE TRAIL IS DECIDED RATHER THAN INHERITED. `governed/audit.py` gates the
    #: stream by the dedicated logger's LEVEL, and a service that never sets it leaves `workbench.audit`
    #: at NOTSET — inheriting the root level `setup_logging` takes from `WORKBENCH_LOG_LEVEL`. Measured
    #: 2026-09-09 across the ten services holding `audit()` call sites: three set it and seven
    #: inherited, so for those seven `WORKBENCH_LOG_LEVEL=WARNING` — the documented volume lever — deleted
    #: the compliance trail, and this flag could not turn it off. `make_service_app` now applies it,
    #: for the same reason it applies the readiness flags: a convention most apps did not hold up is
    #: not a convention. Pinned by
    #: `test_the_audit_trail_is_armed_by_configuration_not_by_the_log_level.py`.
    audit_enabled: bool = Field(default=True, alias="WORKBENCH_AUDIT_ENABLED")

    #: The request-body ceiling every app built by `make_service_app` enforces (`body_limit.py`).
    #:
    #: A DoS BOUND, not a business rule. It exists so a multi-GB body cannot reach a buffer — five
    #: apps had no ceiling at all until 2026-08-26. Generous on purpose: a route that wants a real limit states its own,
    #: and the catalog sets 256 MiB explicitly for Arrow-IPC writes (it builds its own app, so it is
    #: not double-capped).
    max_body_bytes: int = Field(default=64 * 1024 * 1024, alias="WORKBENCH_MAX_BODY_BYTES")

    #: Whether this app exports its traces, metrics and logs over OTLP/HTTP, to the endpoint the SDK's own
    #: `OTEL_EXPORTER_OTLP_*` variables name. Off by default, and off whatever else the environment holds
    #: (`service_kit.otel.setup_otel`).
    otel_enabled: bool = Field(default=False, alias="WORKBENCH_OTEL_ENABLED")
    #: The mounted file holding the token every OTLP export presents as its bearer: the pod's projected ServiceAccount
    #: token for the gateway's audience, read on every export because the kubelet replaces it before it expires. Unset,
    #: exports carry no credential, for a gateway that asks none.
    otel_token_file: Path | None = Field(default=None, alias="WORKBENCH_OTEL_TOKEN_FILE")
