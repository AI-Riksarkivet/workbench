"""A service module's log record reaches stdout.

Every module logs through ``logging.getLogger(__name__)``, so a handler attached to any narrower logger name would leave
every other module's records propagating to a root logger with no handler, and discarded: a branch that reports a
caught failure with a WARNING would report it nowhere. These tests pin the property rather than the implementation:
``setup_logging`` may attach handlers wherever it likes, as long as a service module's record comes out.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from workbench.app import setup_logging


#: A real service module's logger name. Named explicitly rather than generated, because the defect
#: was precisely that the configured names did not match the used ones.
SERVICE_LOGGERS = [
    "annotator.projects.saga",
]


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Leave global logging exactly as found — this module mutates it, and pytest's own capture
    handlers live on the root logger."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_named = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in ("core", "backends")}
    yield
    root.handlers, root.level = saved_handlers, saved_level
    for name, (handlers, level) in saved_named.items():
        logging.getLogger(name).handlers, logging.getLogger(name).level = handlers, level


def _captured(logger_name: str, level: int = logging.WARNING, *, monkeypatch: pytest.MonkeyPatch) -> str:
    """Emit one record and return what reached the handler `_setup_logging` INSTALLED.

    Capturing through a probe handler of our own would prove nothing — a handler attached to root
    receives propagated records whether or not setup did its job, which is exactly how the first
    version of this test passed against the broken code. So: strip every existing handler, point
    `sys.stdout` at a buffer, and let setup build its own handler over that buffer.
    """
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    root = logging.getLogger()
    root.handlers = []
    for name in ("core", "backends"):
        logging.getLogger(name).handlers = []

    setup_logging("INFO")

    logging.getLogger(logger_name).log(level, "canary-%s", logger_name)
    for handler in root.handlers:
        handler.flush()
    return stream.getvalue()


@pytest.mark.parametrize("name", SERVICE_LOGGERS)
def test_a_service_module_logger_REACHES_a_handler(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the workbench actually needs: a module's log record comes out on stdout."""
    assert f"canary-{name}" in _captured(name, monkeypatch=monkeypatch), (
        f"a WARNING from {name!r} reached no handler.\n"
        "Every service module uses getLogger(__name__); if setup only configures specific logger "
        "names, records propagate to a root with no handlers and are discarded — which is how a "
        "two-day lineage feed outage produced zero log lines."
    )


def test_the_root_logger_is_configured_at_or_below_INFO() -> None:
    """`log.info` is load-bearing here — `ray_stage_job_submitted` and `transform_spec_set` are INFO,
    and they are how an operator sees that work started at all."""
    setup_logging("INFO")
    root = logging.getLogger()

    assert root.level <= logging.INFO, f"root is at {logging.getLevelName(root.level)}; INFO-level operational logs are discarded"
    assert root.handlers, "the root logger has no handlers — every propagated record is dropped"


def test_setup_is_IDEMPOTENT_so_repeated_app_builds_do_not_duplicate_lines() -> None:
    """`make_service_app` runs it per app, and a test process may build several."""
    setup_logging("INFO")
    first = len(logging.getLogger().handlers)
    setup_logging("INFO")
    setup_logging("INFO")

    assert len(logging.getLogger().handlers) == first, "handlers accumulated — each log line would be emitted N times"
