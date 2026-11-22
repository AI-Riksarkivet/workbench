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
        # (`packages/workbench/tests/test_every_setting_has_one_name.py`).
        validate_by_name=False,
        validate_by_alias=True,
        env_prefix="",
    )

    api_prefix: str = Field(default="/api/v1", alias="WORKBENCH_API_PREFIX")
    #: The origins besides the app's own a browser may call from: a JSON list or a comma-separated string; none by default.
    cors_origins: StringList = Field(default_factory=list, alias="WORKBENCH_CORS_ORIGINS")

    #: The root log level every app built by `make_service_app` logs at (`workbench.app.setup_logging`): the volume
    #: lever. Root-level INFO makes chatty third-party libraries audible too, the trade against losing an application log.
    log_level: str = Field(default="INFO", alias="WORKBENCH_LOG_LEVEL")

    #: Whether this app serves `/docs`, `/redoc` and `/openapi.json`. OFF by default, and the default
    #: is the whole point: a security default every deployment must remember to turn off is one nobody turns off, and a
    #: flag no deployment path sets IS its default.
    #:
    #: What this closes is a route table, parameter names and request/response schemas — not data, and
    #: every documented route stays individually auth-gated.
    docs_enabled: bool = Field(default=False, alias="WORKBENCH_DOCS")

    #: Whether this app emits the `workbench.audit` compliance stream. ON by default, because the trail
    #: it gates is evidence: a deployment that wants none says so, and one that forgets still has it.
    #:
    #: THE FLAG EXISTS SO THE TRAIL IS DECIDED RATHER THAN INHERITED. `governed/audit.py` gates the
    #: stream by the dedicated logger's LEVEL, and a logger nobody sets stays at NOTSET, inheriting the root level
    #: `log_level` sets: `WORKBENCH_LOG_LEVEL=WARNING` would then delete the trail. Measured 2026-09-09 across the ten
    #: services then holding `audit()` call sites: three set the level and seven inherited it. `make_service_app` applies
    #: this flag to every app it builds. Pinned by
    #: `test_the_audit_trail_is_armed_by_configuration_not_by_the_log_level.py`.
    audit_enabled: bool = Field(default=True, alias="WORKBENCH_AUDIT_ENABLED")

    #: The request-body ceiling every app built by `make_service_app` enforces (`body_limit.py`).
    #:
    #: A DoS BOUND, not a business rule. It exists so a multi-GB body cannot reach a buffer. Generous on purpose: a
    #: service that takes larger or smaller bodies sets its own (the viewer and search 32 MiB, the annotator 16 MiB).
    max_body_bytes: int = Field(default=64 * 1024 * 1024, alias="WORKBENCH_MAX_BODY_BYTES")

    #: Whether this app exports its traces, metrics and logs over OTLP/HTTP, to the endpoint the SDK's own
    #: `OTEL_EXPORTER_OTLP_*` variables name. Off by default, and off whatever else the environment holds
    #: (`workbench.otel.setup_otel`).
    otel_enabled: bool = Field(default=False, alias="WORKBENCH_OTEL_ENABLED")
    #: The mounted file holding the token every OTLP export presents as its bearer: the pod's projected ServiceAccount
    #: token for the gateway's audience, read on every export because the kubelet replaces it before it expires. Unset,
    #: exports carry no credential, for a gateway that asks none.
    otel_token_file: Path | None = Field(default=None, alias="WORKBENCH_OTEL_TOKEN_FILE")
