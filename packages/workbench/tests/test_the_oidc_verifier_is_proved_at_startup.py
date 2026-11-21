"""`OIDC verifier ready` must be a measurement, not an assertion.

Found by the same external comparison as the 503 fix beside it
(`docs/audits/2026-09-20-polaris-k8s-comparison.md`): the upstream operator resolves its auth plane
once at startup and carries the verdict, rather than discovering it on the first request.

WHAT THE LINE CLAIMED. `attach_governed_auth` constructs `OIDCVerifier(...)` and logs
``"%s: OIDC verifier ready (issuer=%s)"``. The constructor performs no I/O at all — it normalises the
issuer list and stores settings. Discovery and JWKS are fetched lazily by `_resolve`, on the first
request that presents a bearer. So a pod with a wrong `WORKBENCH_OIDC_ISSUER`, a wrong
`WORKBENCH_OIDC_DISCOVERY_URL` (the split-horizon override) or an unreachable IdP comes up logging "ready", reports healthy, and the
fault appears only as failed user traffic.

Its `except` compounded the claim: "OIDC verifier failed to build — governed routes will 503" covers
a CONSTRUCTION failure, and the only construction failure the constructor can raise is an empty
issuer list, which `GovernedAuthSettings` already refuses. The branch could not fire for the reason
it named.

WARMING IS THE PROOF, AND IT MUST NOT GATE READINESS. Two things are being separated here:

  * warm at startup, so the misconfiguration is in the pod's log at boot with the URL that failed,
    and so the first real request does not pay the discovery round-trip;
  * do NOT fail startup or `/readyz` on it. Readiness that gates on a downstream dependency turns one
    IdP blip into every governed pod leaving its Service endpoints at once — a self-inflicted
    outage strictly worse than the honest 503 the door now answers. The pod stays up and says so.

So: warming reports, it does not decide. `warm()` returns the issuers that failed and their reason
rather than raising, because the caller is the only thing that knows whether this deployment treats
an unreachable IdP as fatal, and today none of them do.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager

import httpx
import pytest

from workbench.governed import oidc


ISSUER = "https://idp.example.test"
SECOND = "https://other.example.test"


def _ok_document(issuer: str) -> dict[str, object]:
    return {"issuer": issuer, "jwks_uri": f"{issuer}/jwks", "id_token_signing_alg_values_supported": ["RS256"]}


def _answer_reads(monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], httpx.Response]) -> None:
    """Every read of an issuer's document (each is streamed, its body read raw) answered by ``answer`` for its URL."""

    @contextmanager
    def _stream(self: httpx.Client, method: str, url: str, **_kw: object) -> Generator[httpx.Response]:
        answered = answer(url)
        yield httpx.Response(answered.status_code, stream=httpx.ByteStream(answered.content), request=answered.request)

    monkeypatch.setattr(httpx.Client, "stream", _stream)


def test_warm_tries_EVERY_configured_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-issuer deployment is exactly where one misconfigured entry hides: the other issuer's
    traffic works, so nothing looks wrong until a user from the broken tenant signs in."""
    seen: list[str] = []

    # THE BROKEN ONE IS FIRST, deliberately. With the healthy issuer first, a `warm` that stopped at
    # the first failure would still try both and this test would pass against it — measured: that
    # mutation survived the original ordering.
    def _second_only(url: str) -> httpx.Response:
        seen.append(url)
        if url.startswith(SECOND):
            return httpx.Response(200, json=_ok_document(SECOND), request=httpx.Request("GET", url))
        raise httpx.ConnectError("connection refused")

    _answer_reads(monkeypatch, _second_only)

    verifier = oidc.OIDCVerifier([ISSUER, SECOND], "rask", 300)
    try:
        failures = dict(verifier.warm())
    finally:
        verifier.close()

    # The first issuer's discovery failed; the second's was read, and its key set (a discovery document, no key in it)
    # was tried after it.
    assert (failures[ISSUER].startswith("The OIDC issuer could not be reached"), "key set" in failures[SECOND]) == (True, True)
    assert seen == [f"{ISSUER}/.well-known/openid-configuration", f"{SECOND}/.well-known/openid-configuration", f"{SECOND}/jwks"], (
        f"warm stopped at the first failure and never tried the rest: {seen}"
    )


def test_a_warmed_verifier_does_not_refetch_on_the_first_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second reason to warm. Warming makes the refresher's first read before any request, so no
    request pays the discovery round-trip — a latency spike that lands on whoever happens to sign in
    after a rollout."""
    calls: list[str] = []

    def _count(url: str) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, json=_ok_document(ISSUER), request=httpx.Request("GET", url))

    _answer_reads(monkeypatch, _count)
    verifier = oidc.OIDCVerifier(ISSUER, "rask", 300)
    try:
        verifier.warm()
        before = len(calls)

        with pytest.raises(Exception):  # noqa: B017 — the token is junk; only the FETCH COUNT is under test
            verifier.verify("not-a-token")
    finally:
        verifier.close()

    assert (before > 0, len(calls)) == (True, before), f"discovery was re-fetched after warming: {calls[before:]}"
