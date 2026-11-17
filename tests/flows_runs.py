"""What the flows service's offline tests share: its state settings, a poll, and a Serve stand-in on the loopback.

`ServeStandIn` is for a test whose claim crosses a process (a flows process killed and started again) or two drivers:
it records every call it takes, by app and `Idempotency-Key`, and an app can be held until the test lets it answer.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    import httpx

    from tests.postgres import StateDatabase


#: How long a test waits for a run to end, or for the stand-in to be reached.
WAIT_SECONDS = 30.0


def state_env(database: StateDatabase) -> dict[str, str]:
    """The flows state settings for ``database``: a loopback server, so no TLS and no password."""
    return {
        "WORKBENCH_FLOWS_STATE_HOST": database.host,
        "WORKBENCH_FLOWS_STATE_PORT": str(database.port),
        "WORKBENCH_FLOWS_STATE_DATABASE": database.database,
        "WORKBENCH_FLOWS_STATE_USER": database.user,
        "WORKBENCH_FLOWS_STATE_SSLMODE": "disable",
        # A short lease and a quick re-drive, so a run whose driver stopped is resumed within a test's patience.
        "WORKBENCH_FLOWS_RUN_LEASE_SECONDS": "2",
        "WORKBENCH_FLOWS_RUN_REDRIVE_SECONDS": "0.2",
    }


def until(check: Callable[[], Any], what: str, *, timeout: float = WAIT_SECONDS) -> Any:
    """Poll ``check`` until it answers something truthy, and answer that; fail naming ``what`` past the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(0.05)
    raise AssertionError(f"{what} within {timeout:.0f} s")


def ended(client: httpx.Client, project: str, run_id: str) -> dict[str, Any]:
    """The run, once it has ended, read through ``client`` (a `TestClient`, or an `httpx.Client` on a running flows)."""

    def read() -> dict[str, Any] | None:
        run = client.get(f"/api/flows/projects/{project}/runs/{run_id}").json()
        return run if run["status"] != "running" else None

    return until(read, f"run {run_id} did not end")


class ServeStandIn:
    """A Serve ingress on the loopback that answers `POST /<app>` with ``<app>(<body>)`` and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._held: dict[str, threading.Event] = {}
        self._reached: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                app = self.path.lstrip("/").split("?", 1)[0]
                body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                with stand_in._lock:
                    stand_in.calls.append((app, self.headers.get("Idempotency-Key")))
                    held, reached = stand_in._held.get(app), stand_in._reached.setdefault(app, threading.Event())
                reached.set()
                if held is not None:
                    held.wait(WAIT_SECONDS)
                answer = f"{app}({body})".encode()
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.send_header("content-length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib's own parameter name
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def hold(self, app: str) -> None:
        """Keep every call to ``app`` unanswered until `release`."""
        with self._lock:
            self._held[app] = threading.Event()

    def release(self, app: str) -> None:
        with self._lock:
            held = self._held.pop(app, None)
        if held is not None:
            held.set()

    def reached(self, app: str) -> bool:
        with self._lock:
            return self._reached.setdefault(app, threading.Event()).is_set()

    def calls_to(self, app: str) -> list[str | None]:
        """The idempotency key of every call ``app`` took, in order."""
        with self._lock:
            return [key for called, key in self.calls if called == app]

    def close(self) -> None:
        with self._lock:
            held = list(self._held.values())
        for event in held:
            event.set()
        self._server.shutdown()
        self._server.server_close()
