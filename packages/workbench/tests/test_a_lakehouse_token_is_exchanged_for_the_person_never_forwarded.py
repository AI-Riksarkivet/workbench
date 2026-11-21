"""A token addressed to the lakehouse is the provider's answer to an RFC 8693 exchange of the person's own token.

Against the loopback provider (`tests.identity.Provider`) over a real socket: the service authenticates as its own
confidential client with the secret its mounted file holds, the request carries the RFC 8693 parameters, the answer is
cached for the person until shortly before it expires, a refusal is the person's to fix (401), and the workbench's own
token for its writes is a client-credentials grant, asking for the audience's scope at a provider that reads one (Dex).
"""

from __future__ import annotations

import base64
from pathlib import Path

import jwt
import pytest

from tests.identity import CLIENT_ID, CLIENT_SECRET, LAKEHOUSE_AUDIENCE, LAKEHOUSE_AUDIENCE_SCOPE, BrowserKey, Provider, remember_verified
from workbench.exceptions import ServiceUnavailableError, UnauthorizedError
from workbench.governed.token_exchange import EXPIRY_MARGIN_SECONDS, LakehouseTokens, PersonTokens


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tokens(provider: Provider, tmp_path: Path, clock: _Clock) -> LakehouseTokens:
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET + "\n")
    return LakehouseTokens(issuer=provider.issuer, audience=LAKEHOUSE_AUDIENCE, client_id=CLIENT_ID, client_secret_file=str(secret), clock=clock)


def test_a_persons_token_is_held_no_longer_than_its_own_expiry_whether_or_not_it_is_read_again() -> None:
    clock = _Clock()
    people = PersonTokens(clock=clock)
    people.remember("keycloak~gina", issuer="https://id.example", token="gina-token", expires_at=1010.0)
    people.remember("keycloak~kim", issuer="https://id.example", token="kim-token", expires_at=1010.0)
    clock.now = 1011.0

    with pytest.raises(UnauthorizedError):
        people.latest("keycloak~gina", issuer="https://id.example")
    people.remember("keycloak~olga", issuer="https://id.example", token="olga-token", expires_at=1100.0)

    assert len(people) == 1


def test_a_persons_token_is_exchanged_once_per_lifetime_as_rfc_8693_asks(oidc_provider: Provider, tmp_path: Path) -> None:
    clock = _Clock()
    tokens = _tokens(oidc_provider, tmp_path, clock)
    workbench_token = oidc_provider.mint("gina", jkt=BrowserKey().jkt)
    remember_verified(tokens, "keycloak~gina", workbench_token)

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
    remember_verified(tokens, "keycloak~gina", oidc_provider.mint("gina"))
    oidc_provider.refuse_exchange = True

    with pytest.raises(UnauthorizedError, match="refused to exchange") as refused:
        tokens.for_person("keycloak~gina")
    own = jwt.decode(tokens.for_service(), options={"verify_signature": False})

    # The person's session to renew, challenged as the door challenges it, so the app asks them to sign in again.
    assert 'error_description="the session must be renewed"' in (refused.value.headers or {}).get("WWW-Authenticate", "")
    assert (own["sub"], own["aud"]) == (f"service-account-{CLIENT_ID}", LAKEHOUSE_AUDIENCE)
    assert [r["grant_type"] for r in oidc_provider.requests] == ["urn:ietf:params:oauth:grant-type:token-exchange", "client_credentials"]


def test_at_a_dex_reached_in_cluster_the_services_own_token_is_asked_there_with_the_audiences_scope(tmp_path: Path) -> None:
    """The development cluster's Dex: its documents name the public issuer the browser reaches (`localhost:8080/dex`),
    which a pod cannot reach, so the token is asked at the in-cluster address the discovery was fetched from."""
    dex = Provider("dex", public_issuer="http://localhost:9/dex")
    secret = tmp_path / "client-secret"
    secret.write_text(CLIENT_SECRET)
    tokens = LakehouseTokens(
        issuer=dex.issuer,
        audience=LAKEHOUSE_AUDIENCE,
        client_id=CLIENT_ID,
        client_secret_file=str(secret),
        audience_scope=LAKEHOUSE_AUDIENCE_SCOPE,
        discovery_url=dex.address,
    )
    try:
        own = jwt.decode(tokens.for_service(), options={"verify_signature": False})
    finally:
        tokens.close()
        dex.close()

    assert (own["aud"], own["azp"]) == ([LAKEHOUSE_AUDIENCE, CLIENT_ID], CLIENT_ID)


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
    remember_verified(tokens, "keycloak~gina", oidc_provider.mint("gina"))

    with pytest.raises(ServiceUnavailableError, match="did not issue a token"):
        tokens.for_person("keycloak~gina")
