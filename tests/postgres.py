"""The pinned PostgreSQL server on loopback, for the offline suites that hold the annotator's state.

The server tree is the one the Dagger module pins (`.dagger/main.go`'s `PostgresImage`), assembled by
`scripts/postgres-server.sh`: `dagger call test` mounts it and names it in `WORKBENCH_POSTGRES_DIR`, and `make postgres`
exports it to `.localbin/postgres`. Missing, the fixture fails rather than skips, so a lane without it reports the gap.

One server per xdist worker, on a free loopback port with trust authentication (nothing beyond the loopback reaches it)
and durability off (`fsync`): a test's data lives as long as the test. Every test takes a database of its own on it
(`StateDatabase`), so no test reads another's rows.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import psycopg


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def postgres_tree() -> Path:
    """The pinned server tree, from `WORKBENCH_POSTGRES_DIR` or `.localbin/postgres`; a missing one fails the test."""
    tree = Path(os.environ.get("WORKBENCH_POSTGRES_DIR") or Path(__file__).resolve().parent.parent / ".localbin" / "postgres")
    if not os.access(tree / "bin" / "postgres", os.X_OK):
        raise RuntimeError(f"no PostgreSQL server tree at {tree}: `make postgres` exports the pinned one")
    return tree


def server_env(tree: Path) -> dict[str, str]:
    """The environment the tree's binaries run in: the shared libraries it carries, ahead of the host's."""
    return {**os.environ, "LD_LIBRARY_PATH": str(tree / "deps")}


class PostgresServer:
    """One PostgreSQL process over a fresh data directory; a port another worker took in between is retried."""

    user = "postgres"

    def __init__(self, tree: Path, workdir: Path) -> None:
        env = server_env(tree)
        data = workdir / "data"
        subprocess.run(  # noqa: S603 - argv is a literal around the pinned binary
            [str(tree / "bin" / "initdb"), "-D", str(data), "-U", self.user, "--auth=trust", "--encoding=UTF8", "--locale=C", "--no-sync"],
            env=env,
            check=True,
            capture_output=True,
        )
        failures: list[str] = []
        for attempt in range(3):
            port = _free_port()
            log = workdir / f"postgres-{attempt}.log"
            process = subprocess.Popen(  # noqa: S603 - argv is a literal around the pinned binary
                [
                    str(tree / "bin" / "postgres"),
                    "-D",
                    str(data),
                    "-p",
                    str(port),
                    "-c",
                    "listen_addresses=127.0.0.1",
                    # No socket file: its path would sit under the test's temp dir, past the 107-byte limit.
                    "-c",
                    "unix_socket_directories=",
                    "-c",
                    "fsync=off",
                    "-c",
                    "full_page_writes=off",
                    "-c",
                    "synchronous_commit=off",
                    "-c",
                    "max_connections=200",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=log.open("wb"),
            )
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with psycopg.connect(host="127.0.0.1", port=port, user=self.user, dbname="postgres", connect_timeout=1):
                        self._process, self.port = process, port
                        return
                except psycopg.OperationalError:
                    time.sleep(0.05)
            process.kill()
            process.wait(timeout=10)
            failures.append(log.read_text(errors="replace")[-2000:] or f"no connection on 127.0.0.1:{port} within 30 s")
        raise RuntimeError(f"PostgreSQL did not start: {failures}")

    def create_database(self) -> str:
        """A database of one test's own."""
        name = f"test_{uuid.uuid4().hex}"
        with psycopg.connect(host="127.0.0.1", port=self.port, user=self.user, dbname="postgres", autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"'.encode())
        return name

    def close(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=20)


class StateDatabase:
    """One test's database on the worker's server, named as the annotator's settings name it."""

    def __init__(self, server: PostgresServer) -> None:
        self.host = "127.0.0.1"
        self.port = server.port
        self.user = server.user
        self.database = server.create_database()

    def env(self) -> dict[str, str]:
        """The annotator's state settings for this database: a loopback server, so no TLS and no password."""
        return {
            "ANNOTATOR_STATE_HOST": self.host,
            "ANNOTATOR_STATE_PORT": str(self.port),
            "ANNOTATOR_STATE_DATABASE": self.database,
            "ANNOTATOR_STATE_USER": self.user,
            "ANNOTATOR_STATE_SSLMODE": "disable",
        }
