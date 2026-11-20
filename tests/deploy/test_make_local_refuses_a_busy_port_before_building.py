"""`make local` refuses a port something already listens on before it installs or builds anything.

A build replaces the app files a running stack serves, so a second `make local` started beside a running one must stop
at the port check, not after `bun run build`. Driven as an operator runs it: `make local` with one of the stack's ports
taken, and the tools the later steps would call (uv, bun, dagger, prek) standing in as programs that record being run.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("uv", "bun", "dagger", "prek")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def taken() -> Iterator[int]:
    """A port something listens on, on the loopback."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield int(listener.getsockname()[1])


def test_make_local_with_a_port_taken_names_it_and_builds_nothing(taken: int, tmp_path: Path) -> None:
    called = tmp_path / "called"
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in TOOLS:
        (tools / tool).write_text(f'#!/bin/sh\necho {tool} >> "{called}"\nexit 1\n')
        (tools / tool).chmod(0o755)
    env = os.environ | {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "EDGE_PORT": str(taken),
        **{name: str(_free_port()) for name in ("VIEWER_PORT", "SEARCH_PORT", "ANNOTATOR_PORT", "FLOWS_PORT", "STATE_PORT", "MODEL_PORT")},
        "PID_FILE": str(tmp_path / "stack.pid"),
        "TMPDIR": str(tmp_path),
    }

    run = subprocess.run(["make", "-C", str(ROOT), "local"], env=env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S607 - make from PATH

    assert run.returncode != 0
    assert f"something already listens on port {taken} (edge)" in run.stderr
    assert not called.exists(), called.read_text()
