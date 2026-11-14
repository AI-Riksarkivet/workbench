"""The workbench's live harness: a rask lakehouse reached through its public doors only.

What the live suites need of a deployed lakehouse, written against rask's published contract (`docs/rask/`):
- a person signed in at the deployment's Dex with the password grant;
- the principal the catalog derives for a bearer (`GET /management/v1/me`);
- a scratch tenant (project, warehouse and a bronze namespace) made and removed through the catalog's doors;
- lineage's commit feed, followed forward by cursor (`docs/rask/COMMIT-EVENTS.md`).

Every request closes its connection: through `kubectl port-forward` a server's close of an idle keep-alive connection
reaches the client only as its next request fails, and a POST must not be retried (RFC 9112 § 9.3.1).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import lance_namespace
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "").rstrip("/")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ADMIN_USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")
JOB_USER = os.environ.get("LANCE_E2E_PIPELINE_USER", "publisher@rask.internal")
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "120"))

#: The client a suite signs in as, and the lakehouse's audience every governed door requires: `federated:id` makes
#: Dex report the connector and user id the catalog keys a person on, and the cross-client scope puts the audience in
#: the token.
DEX_CLIENT = os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "rask-web")
DEX_AUDIENCE = os.environ.get("LANCE_E2E_OIDC_AUDIENCE", "rask-lakehouse")
DEX_SCOPE = f"openid email federated:id audience:server:client_id:{DEX_AUDIENCE}"

CLOSE_EACH_REQUEST = "close"


def principal_of(bearer: str) -> str:
    """The principal key the catalog derives for ``bearer``."""
    answer = requests.get(f"{CATALOG}/management/v1/me", headers={"authorization": f"Bearer {bearer}"}, timeout=30)
    assert answer.status_code == 200, f"GET /management/v1/me -> {answer.status_code}: {answer.text[:300]}"
    return str(answer.json()["sub"])


class Identity:
    """One caller of the lakehouse's doors: its bearer, and the principal the lakehouse's grants name."""

    def __init__(self, bearer: str) -> None:
        self.bearer = bearer
        self.principal = principal_of(bearer)
        self.http = requests.Session()
        self.http.headers["authorization"] = f"Bearer {bearer}"
        self.http.headers["connection"] = CLOSE_EACH_REQUEST

    @classmethod
    def person(cls, email: str) -> Identity:
        """A Dex user, signed in with the password grant."""
        answer = requests.post(
            f"{DEX}/token",
            data={
                "grant_type": "password",
                "client_id": DEX_CLIENT,
                "client_secret": DEX_SECRET,
                "username": email,
                "password": "password",
                "scope": DEX_SCOPE,
            },
            timeout=20,
        )
        answer.raise_for_status()
        return cls(str(answer.json()["id_token"]))

    def namespace(self) -> lance_namespace.LanceNamespace:
        """The stock client an outside pipeline gets."""
        return lance_namespace.connect("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {self.bearer}", "headers.Connection": CLOSE_EACH_REQUEST})


def ok(answer: requests.Response) -> dict[str, Any]:
    """The JSON body of a 200 answer; any other status fails, naming the request."""
    assert answer.status_code == 200, f"{answer.request.method} {answer.request.url} -> {answer.status_code}: {answer.text[:500]}"
    return answer.json()


class Tenant:
    """A scratch tenant one run drives (project, warehouse and its bronze namespace), and what it must take down."""

    def __init__(self, purpose: str, grants: Sequence[str]) -> None:
        self.admin = Identity.person(ADMIN_USER)
        self.job = Identity.person(JOB_USER)
        self.project = f"{purpose}-{uuid.uuid4().hex[:8]}"
        #: A namespace's name begins with its warehouse's id; the warehouse's id is the project's.
        self.bronze = f"{self.project}$bronze"
        self.grants = tuple(grants)
        self.tables: list[str] = []

    def create(self) -> None:
        admin = self.admin
        ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": self.project}))
        ok(admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": self.project, "project": self.project}))
        ok(admin.http.post(f"{CATALOG}/v1/namespace/{self.bronze}/create", json={"properties": {"rask.tier": "bronze"}}))
        for relation in self.grants:
            ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{self.bronze}/access/grant", json={"user": self.job.principal, "relation": relation}))

    def tear_down(self) -> list[str]:
        """Drop every table, then the warehouse with its bucket, then the project; what failed."""
        failed: list[str] = []
        attempts: list[tuple[str, Callable[[], requests.Response]]] = [
            (table, lambda t=table: self.admin.http.post(f"{CATALOG}/v1/table/{t}/drop", json={}, timeout=60)) for table in self.tables
        ]
        attempts.append(
            (
                "warehouse",
                lambda: self.admin.http.delete(
                    f"{CATALOG}/management/v1/warehouses/{self.project}", params={"cascade": "true", "purge_bucket": "true"}, timeout=120
                ),
            )
        )
        attempts.append(("project", lambda: self.admin.http.delete(f"{CATALOG}/management/v1/projects/{self.project}", timeout=60)))
        for what, attempt in attempts:
            answer = attempt()
            if answer.status_code != 200:
                failed.append(f"{what}: {answer.status_code} {answer.text[:200]}")
        return failed


def provisioned(purpose: str, grants: Sequence[str]) -> Iterator[Tenant]:
    """A created tenant for a module fixture to yield, taken down afterwards whatever the drive did; a remnant fails."""
    made = Tenant(purpose, grants)
    try:
        made.create()
        yield made
    finally:
        left = made.tear_down()
        assert not left, f"the scratch tenant {made.project} was not fully removed: {left}"


def lance_facet(row: dict[str, Any]) -> dict[str, Any]:
    """The `rask_lance` facet of a feed row: on the run of a write, on the dataset of a definition change."""
    event = row.get("event") or {}
    carrier = event.get("run") or event.get("dataset") or {}
    return (carrier.get("facets") or {}).get("rask_lance") or {}


def feed_start(identity: Identity) -> int:
    """Where a reader that has read nothing yet starts following: the newest row it may see, else the oldest held."""
    body = ok(identity.http.get(f"{LINEAGE}/events", params={"limit": 1}, timeout=30))
    events = body.get("events") or []
    return int(events[0]["seq"]) if events else max(int(body.get("oldest_seq") or 1) - 1, 0)


def follow(identity: Identity, *, since: int, table: str, until: Callable[[list[dict[str, Any]]], bool]) -> tuple[list[dict[str, Any]], int]:
    """Follow the feed forward from ``since``, passing `next_cursor` back as `since` every time, until the rows read so
    far satisfy ``until``; the rows, and the cursor to resume from."""
    rows: list[dict[str, Any]] = []
    cursor = since
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        body = ok(identity.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": table, "limit": 500}, timeout=30))
        rows += body["events"]
        assert int(body["next_cursor"]) >= cursor, f"the cursor moved backwards: {cursor} -> {body['next_cursor']}"
        cursor = int(body["next_cursor"])
        if until(rows):
            return rows, cursor
        assert time.monotonic() < deadline, f"the feed for {table} did not show what was expected within {SETTLE_SECONDS:.0f} s"
        time.sleep(2)


def rask_properties(properties: Mapping[str, str]) -> dict[str, Any]:
    """The `rask.*` entries of a commit's transaction properties, `rask.inputs` decoded."""
    found: dict[str, Any] = {key: value for key, value in properties.items() if key.startswith("rask.")}
    if "rask.inputs" in found:
        found["rask.inputs"] = json.loads(found["rask.inputs"])
    return found
