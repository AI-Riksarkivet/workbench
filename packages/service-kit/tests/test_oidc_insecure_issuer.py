"""An `http://` issuer must fail at STARTUP, not as a 401 on every request.

open_fastapi-audit — "An `http://` OIDC issuer in production surfaces as an opaque 401 on every
request, and the 'logged distinctly' the code promises does not exist".

`_require_https` refuses at VERIFY time, so an operator who ships `WORKBENCH_OIDC_ISSUER=http://…` without
`WORKBENCH_OIDC_ALLOW_INSECURE` gets a pod that reports healthy and then refuses every bearer, valid or not,
from the first request on.

THE COMMENT CLAIMED OTHERWISE. It said the failure is "surfaced at verify time as an opaque auth
failure but logged distinctly", and `oidc.py` imported no logging at all. A comment asserting a
behaviour the code does not have is worse than none: it is what an operator reads instead of the logs
that were never written.

WHERE IT BELONGS. `GovernedAuthSettings._validate_governed_auth` already exists to "fail fast at
construction rather than fail open at request time", and already validates the adjacent invariants —
issuer and audience present when OIDC is on, OIDC on when FGA is on. A scheme is knowable at exactly
the same moment.

`_require_https` STAYS as the runtime backstop: `jwks_uri` comes from discovery, so the settings
cannot see it, and that path can still only be caught at verify time. It gets the log line its comment
always promised.
"""

from __future__ import annotations

import logging

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings


class _Governed(BaseSettings, GovernedAuthSettings):
    """A minimal concrete carrier for the mixin.

    `GovernedAuthSettings` is deliberately a plain mixin rather than a settings class of its own, so a
    test cannot instantiate it directly. Mixing it into a bare `BaseSettings` exercises the validator
    without dragging in one service's whole config — and keeps this a test of the SHARED rule rather
    than of whichever service happened to be imported.
    """

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


def _settings(**over: object) -> _Governed:
    base = {
        "WORKBENCH_OIDC_ENABLED": True,
        "WORKBENCH_OIDC_ISSUER": "https://issuer.test",
        "WORKBENCH_OIDC_AUDIENCE": "rask",
        "WORKBENCH_OIDC_CONNECTORS": {"local": "dex"},
    }
    return _Governed.model_validate({**base, **over})


def test_the_dev_escape_hatch_still_works() -> None:
    """A local Dex over http is the case the flag exists for; this must not become a hard ban."""
    settings = _settings(WORKBENCH_OIDC_ISSUER="http://dex.local:5556", WORKBENCH_OIDC_ALLOW_INSECURE=True)
    assert settings.oidc_issuer == "http://dex.local:5556"


def test_the_check_is_skipped_when_oidc_is_off() -> None:
    """An issuer nobody verifies against is not a misconfiguration."""
    assert _Governed.model_validate({"WORKBENCH_OIDC_ENABLED": False, "WORKBENCH_OIDC_ISSUER": "http://x"}) is not None


@pytest.mark.parametrize(
    ("over", "refusal"),
    [
        pytest.param({"WORKBENCH_OIDC_ISSUER": "http://issuer.test"}, "HTTPS", id="an-http-issuer"),
        pytest.param({"WORKBENCH_OIDC_CONNECTORS": {}}, "WORKBENCH_OIDC_CONNECTORS is required", id="no-connector-names-anyone"),
        pytest.param({"WORKBENCH_OIDC_CONNECTORS": {"local": "kubernetes"}}, "a machine identity's idp id", id="a-connector-claims-the-clusters-idp-id"),
        pytest.param({"WORKBENCH_OIDC_CONNECTORS": {"local": "dex-client"}}, "a machine identity's idp id", id="a-connector-claims-the-dex-clients-idp-id"),
        pytest.param({"WORKBENCH_OIDC_CONNECTORS": {"local": "Corp~1"}}, "no principal can carry", id="an-idp-id-outside-the-grammar"),
        pytest.param(
            {"WORKBENCH_SA_ISSUER": "https://issuer.test/", "WORKBENCH_SA_AUDIENCE": "rask-catalog"},
            "name one issuer",
            id="the-cluster-and-the-idp-named-as-one-issuer",
        ),
    ],
)
def test_settings_no_bearer_could_pass_are_refused_at_construction(over: dict[str, object], refusal: str) -> None:
    """Each is refused when the settings are built, not met one request at a time: an issuer verified over http answers
    401 to every bearer. A person is the principal their Dex connector maps to ([[LH-063]]), so a map naming nobody, or
    an id no principal can carry, answers 401 to every person; a connector given the cluster's idp id makes its people
    service accounts; and one issuer named for both routes every person's token as a service account's."""
    with pytest.raises(ValueError, match=refusal):
        _settings(**over)


def test_the_runtime_backstop_logs_what_its_comment_promises(caplog: pytest.LogCaptureFixture) -> None:
    """`jwks_uri` comes from DISCOVERY, so settings cannot see it and only verify time can catch it.
    That path keeps the check — and now actually logs, which is what the comment always claimed."""
    from service_kit.governed import oidc

    with caplog.at_level(logging.WARNING), pytest.raises(Exception, match="HTTPS"):
        oidc._require_https("http://idp.internal/jwks", label="jwks_uri", allow_insecure=False)

    assert "oidc_insecure_url" in caplog.text, (
        "the comment says the failure is 'logged distinctly' and nothing was logged — a Lance door's 503 redacts it, so only the log names the setting"
    )
