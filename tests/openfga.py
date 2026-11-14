"""The pinned OpenFGA server on loopback, in memory, for the offline suites that assert who may do what.

The binary is the one the Dagger module pins (`.dagger/main.go`'s `OpenfgaImage`): `dagger call test` copies it onto
PATH and `make openfga` exports it to `.localbin/openfga` from the same image. Missing, the fixture fails rather than
skips, so a lane without it reports the gap.

It runs with OpenFGA's check and iterator caches on, as a platform's OpenFGA may for scale, so a test that revokes and
checks again proves the read asked past them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from service_kit.governed import fga


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def openfga_binary() -> str:
    """The pinned server binary on PATH or in `.localbin`; a missing one fails the test that needs it."""
    binary = shutil.which("openfga") or str(Path(__file__).resolve().parent.parent / ".localbin" / "openfga")
    if not os.access(binary, os.X_OK):
        raise RuntimeError(f"no OpenFGA server binary on PATH or at {binary}: `make openfga` exports the pinned one")
    return binary


class OpenFgaServer:
    """One OpenFGA process; two free ports are picked and a port another worker took in between is retried."""

    def __init__(self, binary: str, log_dir: Path) -> None:
        failures: list[str] = []
        for attempt in range(3):
            http_port, grpc_port = _free_port(), _free_port()
            log = log_dir / f"openfga-{attempt}.log"
            process = subprocess.Popen(  # noqa: S603 - argv is a literal around the pinned binary
                [
                    binary,
                    "run",
                    "--datastore-engine=memory",
                    f"--http-addr=127.0.0.1:{http_port}",
                    f"--grpc-addr=127.0.0.1:{grpc_port}",
                    "--playground-enabled=false",
                    "--metrics-enabled=false",
                    "--log-level=warn",
                    "--check-query-cache-enabled",
                    "--check-query-cache-ttl=1h",
                    "--check-iterator-cache-enabled",
                    "--check-iterator-cache-ttl=1h",
                    "--list-objects-iterator-cache-enabled",
                    "--list-objects-iterator-cache-ttl=1h",
                ],
                stdout=subprocess.DEVNULL,
                stderr=log.open("wb"),
            )
            url = f"http://127.0.0.1:{http_port}"
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                        self._process, self.url = process, url
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            process.kill()
            process.wait(timeout=10)
            failures.append(log.read_text(errors="replace")[-2000:] or f"no /healthz answer at {url} within 30 s")
        raise RuntimeError(f"OpenFGA did not start: {failures}")

    def close(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


class OpenFgaStore:
    """A store of one test's own on the server, holding the model the services load, provisioned as the writer does."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.name = f"test-{uuid.uuid4().hex}"
        self.store_id, self.model_id = asyncio.run(fga.provision(url, credential_file=None, store_name=self.name))

    def write(self, *grants: tuple[str, str, str]) -> None:
        """Write ``(user, relation, object)`` tuples straight to the store, as a declaration or a door would."""

        async def _write() -> None:
            authz = fga.Authorizer(fga.make_client(self.url, credential_file=None, store_id=self.store_id, model_id=self.model_id))
            try:
                await authz.write(fga.Grant(user=u, relation=r, object=o) for u, r, o in grants)
            finally:
                await authz.close()

        asyncio.run(_write())

    def tuples(self, obj: str) -> set[tuple[str, str, str]]:
        """Every ``(user, relation, object)`` the store holds on ``obj``."""

        async def _read() -> set[tuple[str, str, str]]:
            authz = fga.Authorizer(fga.make_client(self.url, credential_file=None, store_id=self.store_id, model_id=self.model_id))
            try:
                return {(g.user, g.relation, g.object) for g in await authz.grants_on(obj)}
            finally:
                await authz.close()

        return asyncio.run(_read())
