"""The workbench's grants on a lakehouse are made, listed and revoked at the catalog's doors as rask publishes them.

`scripts/lakehouse_grants.py` runs as an operator runs it (a process, its arguments, a password in a file) against a
stand-in catalog and identity provider on loopback sockets. The catalog checks every request the script sends against
rask's published OpenAPI document (`docs/rask/catalog-openapi.json`): the path is one of its operations, the method is
that operation's, and the body validates against the operation's request schema. It keeps what was granted, so a second
run and a revoke are judged by what it holds.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "docs/rask/catalog-openapi.json").read_text())
PASSWORD = "the-administrators-password"
PRINCIPAL = "dex-client~workbench-services"


def _operation(method: str, path: str) -> dict[str, Any] | None:
    """The spec's operation for ``method`` on ``path``, its `{param}` segments matching any one segment."""
    parts = path.split("/")
    for template, operations in SPEC["paths"].items():
        pattern = template.split("/")
        if len(pattern) == len(parts) and all(p.startswith("{") or p == q for p, q in zip(pattern, parts, strict=True)):
            return operations.get(method.lower())
    return None


class _Catalog:
    """The catalog's doors this script uses, held to the published spec, over a warehouse `acme`."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.requests: list[tuple[str, str, str]] = []
        self.namespaces: set[str] = set()
        self.grants: set[tuple[str, str, str]] = set()
        catalog = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
                del format, args

            def _answer(self, status: int, body: object) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                catalog.requests.append((method, self.path, raw))
                if self.headers.get("authorization") != "Bearer admin-token":
                    self._answer(401, {"error": "unauthenticated"})
                    return
                operation = _operation(method, self.path)
                if operation is None:
                    catalog.violations.append(f"{method} {self.path} is no operation of the spec")
                    self._answer(404, {})
                    return
                schema = ((operation.get("requestBody") or {}).get("content") or {}).get("application/json", {}).get("schema")
                if schema is not None:
                    try:
                        jsonschema.validate(json.loads(raw or "{}"), {**schema, "components": SPEC["components"]})
                    except jsonschema.ValidationError as exc:
                        catalog.violations.append(f"{method} {self.path}: {exc.message}")
                self._answer(*catalog.route(method, self.path, json.loads(raw or "{}")))

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's handler name
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802 - the stdlib's handler name
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, object]:
        parts = path.split("/")
        if method == "GET" and path == "/management/v1/warehouses":
            return 200, [{"id": "acme", "bucket": "acme-bucket", "root_uri": "s3://acme-bucket", "project": "default"}]
        if parts[1:3] == ["v1", "namespace"]:
            namespace = unquote(parts[3])
            if parts[4] == "exists":
                return (200, {}) if namespace in self.namespaces else (404, {"error": "no such namespace", "code": 1})
            self.namespaces.add(namespace)
            return 200, {}
        kind, object_id, action = parts[3], unquote(parts[4]), parts[6]
        if action == "list":
            held = sorted({(relation, user) for k, o, relation in self.grants if (k, o) == (kind, object_id) for user in [PRINCIPAL]})
            return 200, {"object": f"{kind}:{object_id}", "grants": [{"relation": relation, "users": [user]} for relation, user in held]}
        grant = (kind, object_id, body["relation"])
        assert body["user"] == PRINCIPAL
        if action == "grant":
            self.grants.add(grant)
        else:
            self.grants.discard(grant)
        return 200, {"object": f"{kind}:{object_id}", "user": body["user"], "relation": body["relation"], "granted": action == "grant"}

    def close(self) -> None:
        self._server.shutdown()


class _Issuer:
    """An identity provider that answers the administrator's password grant on the workbench's public client."""

    def __init__(self) -> None:
        self.forms: list[str] = []
        issuer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
                del format, args

            def _answer(self, body: object) -> None:
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's handler name
                self._answer({"issuer": issuer.url, "token_endpoint": f"{issuer.url}/token"})

            def do_POST(self) -> None:  # noqa: N802 - the stdlib's handler name
                issuer.forms.append(self.rfile.read(int(self.headers.get("content-length") or 0)).decode())
                self._answer({"access_token": "admin-token", "token_type": "bearer", "expires_in": 300})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()


@pytest.fixture
def lakehouse() -> Iterator[tuple[_Catalog, _Issuer]]:
    catalog, issuer = _Catalog(), _Issuer()
    yield catalog, issuer
    catalog.close()
    issuer.close()


def _run(action: str, catalog: _Catalog, issuer: _Issuer, password_file: Path) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(ROOT / "scripts/lakehouse_grants.py"),
        action,
        "--values",
        str(ROOT / "deploy/values-dev.yaml"),
        "--catalog",
        catalog.url,
        "--issuer",
        issuer.url,
        "--admin",
        "alice@example.com",
        "--password-file",
        str(password_file),
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603 - a literal argv


def test_the_grants_are_made_once_listed_and_revoked_at_the_published_doors(lakehouse: tuple[_Catalog, _Issuer], tmp_path: Path) -> None:
    catalog, issuer = lakehouse
    password_file = tmp_path / "password"
    password_file.write_text(PASSWORD + "\n")

    runs = [_run(action, catalog, issuer, password_file) for action in ("grant", "grant", "list", "revoke", "list")]

    assert [run.returncode for run in runs] == [0] * 5, [run.stderr for run in runs]
    assert catalog.violations == []
    assert catalog.namespaces == {"acme$workbench_annotations"}
    assert [request for request in catalog.requests if request[1].endswith("/create")] == [("POST", "/v1/namespace/acme%24workbench_annotations/create", "{}")]
    assert "writer on namespace acme$workbench_annotations" in runs[0].stdout
    assert f"namespace acme$workbench_annotations: {PRINCIPAL} holds writer" in runs[2].stdout
    assert f"warehouse acme: {PRINCIPAL} holds reader" in runs[2].stdout
    assert catalog.grants == set()
    assert f"namespace acme$workbench_annotations: {PRINCIPAL} holds nothing" in runs[4].stdout
    # The password reaches the identity provider's token endpoint, and nothing else, and no run prints a token.
    assert all(PASSWORD in form for form in issuer.forms)
    assert not any(PASSWORD in raw for _method, _path, raw in catalog.requests)
    assert not any("admin-token" in run.stdout + run.stderr for run in runs)
