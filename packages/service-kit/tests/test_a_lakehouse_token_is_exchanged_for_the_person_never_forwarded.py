"""A token addressed to the lakehouse is the provider's answer to an RFC 8693 exchange of the person's own token.

Against the loopback provider (`tests.identity.Provider`) over a real socket: the service authenticates as its own
confidential client with the secret its mounted file holds, the request carries the RFC 8693 parameters, the answer is
cached for the person until shortly before it expires, a refusal is the person's to fix (401), and the workbench's own
token for its writes is a client-credentials grant.
"""

from __future__ import annotations

import base64
from pathlib import Path

import jwt
import pytest

from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError
from service_kit.governed.token_exchange import EXPIRY_MARGIN_SECONDS, LakehouseTokens
from tests.identity import CLIENT_ID, CLIENT_SECRET, LAKEHOUSE_AUDIENCE, BrowserKey, Provider


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tokens(provider: Provider, tmp_path: Path, clock: _Clock) -> LakehouseTokens:
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET + "\n")
    return LakehouseTokens(issuer=provider.issuer, audience=LAKEHOUSE_AUDIENCE, client_id=CLIENT_ID, client_secret_file=str(secret), clock=clock)


def test_a_persons_token_is_exchanged_once_per_lifetime_as_rfc_8693_asks(oidc_provider: Provider, tmp_path: Path) -> None:
    clock = _Clock()
    tokens = _tokens(oidc_provider, tmp_path, clock)
    workbench_token = oidc_provider.mint("gina", jkt=BrowserKey().jkt)
    tokens.remember("keycloak~gina", workbench_token)

    first = tokens.for_person("keycloak~gina")
    cached = tokens.for_person("keycloak~gina")
    clock.now += 300 - EXPIRY_MARGIN_SECONDS
    tokens.for_person("keycloak~gina")

    assert first == cached
    assert jwt.decode(first, options={"verify_signature": False})["aud"] == LAKEHOUSE_AUDIENCE
    basic = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert (
        oidc_provider.requests
        == [
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": workbench_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": LAKEHOUSE_AUDIENCE,
                "authorization": basic,
            }
        ]
        * 2
    )


def test_a_refused_exchange_is_the_persons_401_and_the_service_takes_its_own_token(oidc_provider: Provider, tmp_path: Path) -> None:
    tokens = _tokens(oidc_provider, tmp_path, _Clock())
    tokens.remember("keycloak~gina", oidc_provider.mint("gina"))
    oidc_provider.refuse_exchange = True

    with pytest.raises(UnauthorizedError, match="refused to exchange"):
        tokens.for_person("keycloak~gina")
    own = jwt.decode(tokens.for_service(), options={"verify_signature": False})

    assert (own["sub"], own["aud"]) == (f"service-account-{CLIENT_ID}", LAKEHOUSE_AUDIENCE)
    assert [r["grant_type"] for r in oidc_provider.requests] == ["urn:ietf:params:oauth:grant-type:token-exchange", "client_credentials"]


@pytest.mark.parametrize(
    ("secret", "audience"),
    [pytest.param("not-the-secret", LAKEHOUSE_AUDIENCE, id="invalid-client"), pytest.param(CLIENT_SECRET, "elsewhere", id="invalid-target")],
)
def test_a_refusal_of_the_services_own_client_or_configuration_is_its_outage_not_the_persons_401(
    oidc_provider: Provider, tmp_path: Path, secret: str, audience: str
) -> None:
    secret_file = tmp_path / "client-secret"
    secret_file.write_text(secret)
    tokens = LakehouseTokens(issuer=oidc_provider.issuer, audience=audience, client_id=CLIENT_ID, client_secret_file=str(secret_file))
    tokens.remember("keycloak~gina", oidc_provider.mint("gina"))

    with pytest.raises(ServiceUnavailableError, match="did not issue a token"):
        tokens.for_person("keycloak~gina")
