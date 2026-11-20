"""An unhandled exception must answer problem+json, and a governed error must not read as a crash.

open_fastapi-audit — "Four fleet apps install no catch-all, so any unhandled exception answers
text/plain — a third error envelope no client can parse".

TWO THINGS, and the second is the load-bearing one.

**The catch-all.** `register_handlers` mapped exactly two classes — `DomainError` and
`RequestValidationError` — so anything else fell through to starlette's default: a `text/plain` 500
reading "Internal Server Error". That is the framework default for any app with no `Exception`
handler, so on its own it is not a rask choice. It is still a third envelope on services whose every
other error is RFC 9457, and the fix is one handler in the place the whole fleet already shares.

**The one that actually misreports.** An app built by `make_service_app` alone cannot map
`lance_namespace` errors at all — and the shared governed kernel it calls RAISES them: `governed/fga.py`
raises `ServiceUnavailableError("authorization service unavailable")` under a docstring promising
"outage → ServiceUnavailableError → 503". Without the handlers this file pins, that contract silently
does not hold on such an app. It still fails CLOSED — the refusal happens, nothing is disclosed, no door
opens — so this is a misreported outage, not an exposure. But one function can then answer the same
class of failure two different ways: a `service_kit.exceptions.ServiceUnavailableError` becomes a 503
problem+json while the one `fga.batch_check` raises becomes a text/plain 500.

The factory installs both handler sets, because a denial must be a 403, not a 500. Doing it in the factory rather than
per app also settles the divergence `docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate
audit"` X11 files, one app's 422 differing from its fleet siblings', by making every app the same shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError as NsServiceUnavailableError

from service_kit import make_service_app
from service_kit.context import DiagnosticFormatter
from service_kit.exceptions import ServiceUnavailableError, register_handlers


def _client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False: we are asserting on the RESPONSE the client sees, which is the
    # whole point. With it on, TestClient re-raises and there is no envelope to inspect.
    return TestClient(app, raise_server_exceptions=False)


def test_the_catch_all_leaks_no_internals() -> None:
    """The reference is explicit: internals leak via logs only, never the body."""
    app = FastAPI()
    register_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("/var/lib/rask/creds")

    body = _client(app).get("/boom").text
    assert "/var/lib/rask/creds" not in body, f"the 500 body carried the exception text: {body}"
    assert "Traceback" not in body


def _factory_app(exc: Exception) -> FastAPI:
    app = make_service_app(title="probe", routers=[])

    @app.get("/raise")
    async def _raise() -> None:
        raise exc

    return app


def test_an_fga_outage_reads_as_503_not_500() -> None:
    """The contract `governed/fga.py` documents — "outage → ServiceUnavailableError → 503" — must
    actually hold in the apps that call it, or an outage is indistinguishable from a crash."""
    response = _client(_factory_app(NsServiceUnavailableError("authorization service unavailable"))).get("/raise")
    assert response.status_code == 503, (
        f"an FGA outage answered {response.status_code}; monitoring cannot tell a dependency being down from this service being broken"
    )


#: A detail carrying an upstream's own text, with a newline that would start a forged log line if written raw.
FORGING_DETAIL = "voiceprint runner refused the embed (502): bad gateway\n2026-11-20 12:00:00,000 ERROR   forged — line"


def _chosen() -> Exception:
    return ServiceUnavailableError(FORGING_DETAIL, headers={"Retry-After": "5"})


def _from_a_cause() -> Exception:
    try:
        raise OSError("the store hung up")
    except OSError as exc:
        try:
            raise ServiceUnavailableError("the storage backing 't' is unreadable right now") from exc
        except ServiceUnavailableError as raised:
            return raised


def _crash() -> Exception:
    return RuntimeError("a real crash")


def _factory() -> FastAPI:
    return make_service_app(title="probe", routers=[])


def _fleet_handlers_alone() -> FastAPI:
    """An app with the fleet's handlers and not the Lance taxonomy's: where `lance_namespace` is absent, the fleet's own
    catch-all answers a crash."""
    app = FastAPI()
    register_handlers(app)
    return app


@pytest.mark.parametrize(
    ("raised", "build", "logged"),
    [
        (_chosen, _factory, (logging.WARNING, "service_unavailable", False)),
        (_from_a_cause, _factory, (logging.ERROR, "domain error", True)),
        (_crash, _factory, (logging.ERROR, "unhandled_error", True)),
        (_crash, _fleet_handlers_alone, (logging.ERROR, "unhandled error", True)),
    ],
    ids=["a-chosen-503-is-one-line", "a-503-from-a-cause-keeps-its-traceback", "a-crash-keeps-its-traceback", "a-crash-the-fleet-catch-all-answers"],
)
def test_a_refusal_the_code_chose_is_one_line_and_a_fault_keeps_its_traceback(
    raised: Callable[[], Exception], build: Callable[[], FastAPI], logged: tuple[int, str, bool], caplog: pytest.LogCaptureFixture
) -> None:
    """A service whose issuer restarted answered each read 503 until it read the key set again, and logged a traceback
    for each: 21 in two minutes of one service's log (measured 2026-11-20). A 503 the code chose says all it has to say
    in its detail, rendered escaped, so a newline an upstream's text carries starts no line of its own; a fault's
    traceback is its diagnosis. Every line names the route's template, never the path, which can carry a signed media
    URL's token. The app is the factory's, with every handler a service installs, or the fleet's handlers alone."""
    app = build()

    @app.get("/media/{token}")
    async def _media(token: str) -> None:
        del token
        raise raised()

    caplog.set_level(logging.INFO)
    response = _client(app).get("/media/signed-token-material")

    records = [record for record in caplog.records if record.name.startswith("service_kit") and record.levelno >= logging.WARNING]
    assert [(record.levelno, record.getMessage(), record.exc_info is not None, getattr(record, "route", None)) for record in records] == [
        (*logged, "/media/{token}")
    ]
    assert [name for record in records for name, value in vars(record).items() if "signed-token-material" in str(value)] == []
    one_line = [DiagnosticFormatter("%(levelname)s %(name)s — %(message)s").format(record) for record in records if record.exc_info is None]
    assert [("\n" in line, FORGING_DETAIL in line) for line in one_line] == ([] if logged[2] else [(False, False)])
    assert response.status_code >= 500
