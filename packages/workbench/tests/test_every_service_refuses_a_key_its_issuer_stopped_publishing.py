"""The viewer, search and the annotator refuse alike a token signed by a key their issuer no longer publishes.

Each runs its real app with its real lifespan, which builds its verifier through the one shared seam
(`workbench.governed.auth_lifespan`), against one loopback issuer (`tests.identity.Provider`) that then rotates its key
as Dex on memory storage does at a restart. Measured 2026-11-20 on the dev cluster: after such a restart every viewer read
answered 401 while the annotator kept answering 200 for the same token, because each process kept the key set it last
fetched for its own 300 s. The rule is now one (`KEY_SET_MAX_AGE_SECONDS`, pinned on its clock by
`packages/workbench/tests/test_every_verifier_failure_is_a_401_or_a_503.py`); here its age is zero, so each service's
refresher reads the set again at its next look, and every one of them refuses the token within that.

A door past the verifier answers each service's own way (a source that declares no reader 403, the person's own `/me`
200); a token the verifier refuses is 401 at all three. Each refuses it once a second read running found the set without
its key (a periodic read, or the forced read the token asked for), and every request after that at once.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.identity import IDP_ID, WORKBENCH_AUDIENCE, Provider
from tests.media_services import SERVICES, running
from tests.openfga import OpenFgaStore
from tests.postgres import StateDatabase
from workbench.governed import oidc


#: What each service is asked, by name: a door every person may reach once the verifier lets them in.
DOORS = {"viewer": "/api/datasets/archive~nowhere/descriptor", "search": "/api/search?q=x&dataset=archive~nowhere", "annotator": "/me"}


@pytest.fixture
def services(
    oidc_provider: Provider, openfga_store: OpenFgaStore, state_database: StateDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, TestClient]]:
    shared = {
        "WORKBENCH_SOURCES": [{"kind": "namespace", "name": "archive", "impl": "dir", "properties": {"root": str(tmp_path)}}],
        "WORKBENCH_FGA_ENABLED": True,
        "WORKBENCH_FGA_API_URL": openfga_store.url,
        "WORKBENCH_FGA_STORE": openfga_store.name,
        "WORKBENCH_FGA_MODEL_ID": openfga_store.model_id,
        "WORKBENCH_OIDC_ENABLED": True,
        "WORKBENCH_OIDC_ISSUER": oidc_provider.issuer,
        "WORKBENCH_OIDC_AUDIENCE": WORKBENCH_AUDIENCE,
        "WORKBENCH_OIDC_IDP_ID": IDP_ID,
        "WORKBENCH_OIDC_ALLOW_INSECURE": True,
        "WORKBENCH_DPOP_REQUIRED": False,
        "WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED": False,
    }
    own = {"viewer": shared, "search": shared, "annotator": shared | state_database.env()}
    with ExitStack() as stack:
        yield {service: stack.enter_context(running(service, own[service], monkeypatch)) for service in SERVICES}


def _statuses(services: dict[str, TestClient], token: str) -> dict[str, int]:
    return {name: client.get(DOORS[name], headers={"Authorization": f"Bearer {token}"}).status_code for name, client in services.items()}


def test_a_token_whose_key_the_issuer_dropped_is_refused_by_every_service_alike(
    services: dict[str, TestClient], oidc_provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oidc, "KEY_SET_MAX_AGE_SECONDS", 0.0)
    token = oidc_provider.mint("ada")
    before = _statuses(services, token)
    oidc_provider.rotate()

    # Read on the refreshers' own time: a few looks at most, which a starved host stretches; 30 s only bounds a test that
    # hangs. Every answer each service gave is kept.
    deadline = time.monotonic() + 30
    seen: dict[str, set[int]] = {name: set() for name in services}
    while any(401 not in statuses for statuses in seen.values()) and time.monotonic() < deadline:
        waiting = {name: client for name, client in services.items() if 401 not in seen[name]}
        for name, status in _statuses(waiting, token).items():
            seen[name].add(status)
        time.sleep(0.05)
    # A service that refused the token has read the set without the key twice running (the read that refused it is the
    # second at the latest), so the key is retired there: every later read is refused at once, never told to come back.
    again = [_statuses(services, token) for _ in range(3)]

    # Until its refresher read the new set a service answers as before; from then on, 401: never a 503 between.
    assert (before, {name: statuses - {before[name]} for name, statuses in seen.items()}, again) == (
        {"viewer": 403, "search": 403, "annotator": 200},
        {"viewer": {401}, "search": {401}, "annotator": {401}},
        [{"viewer": 401, "search": 401, "annotator": 401}] * 3,
    )
