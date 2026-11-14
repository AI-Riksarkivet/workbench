"""A dependency being down is not a missing page, and its driver text is not for the browser.

open_fastapi-audit — "viewer's page resolver reports a catalog or object-store OUTAGE as 404 Not Found, with the raw
exception text in the client-visible detail". A 404 is TERMINAL: the zone renders "page not found", the reader stops,
and nothing retries, for a condition that is transient and would clear on its own. And `service_kit.exceptions._problem`
puts `str(exc)` verbatim into the client body at any status, so an interpolated driver message put the internal host
and the ``s3://`` location on the wire.

The page resolver opens a catalog table through lance-namespace as the caller. A catalog that does not answer, or
answers 5xx, is a 503; a table the catalog does not hold stays a 404; a store failure behind a table the catalog
resolved is a 503 whose detail names neither the driver text nor the location. The catalog is driven for real: a
directory catalog, and a REST door that answers 500.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest

from client_kit.media.config import Settings
from client_kit.media.state import AppState
from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from tests.media_catalog import write_table
from viewer.api.v1.endpoints import pages as pages_ep


TABLE: str = "acme$bronze$pages"
LOCATION = "s3://rask-lake/bronze/pages.lance"
SECRET = "connect to object-store.internal:9000: connection refused"


def _state(monkeypatch: pytest.MonkeyPatch, catalog_uri: str) -> AppState:
    monkeypatch.delenv("MEDIA_CATALOG_DIR")
    return AppState(settings=Settings.model_validate({"MEDIA_CATALOG_URI": catalog_uri}))


class _Failing(http.server.BaseHTTPRequestHandler):
    """A catalog that answers every request with the spec's Internal error (code 18) at HTTP 500."""

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's handler name
        body = json.dumps({"error": SECRET, "code": 18}).encode()
        self.send_response(500)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
        return None


@pytest.fixture
def failing_catalog() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Failing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_catalog_outage_is_not_a_missing_page(monkeypatch: pytest.MonkeyPatch, failing_catalog: str) -> None:
    """A transient dependency failure must be retryable, not terminal, and its text stays in the log. An unreachable
    catalog takes the same path: pylance's REST client reports it as the spec's ServiceUnavailable (code 17), one more
    code no caller can act on (measured 2026-11-14 on pylance 13.0.0)."""
    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._open(_state(monkeypatch, failing_catalog), TABLE, "tok")  # noqa: SLF001
    assert not isinstance(caught.value, NotFoundError)
    assert SECRET not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)


def test_an_unreadable_dataset_is_an_outage_not_a_404(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A RustFS outage, expired vended credentials and a corrupt manifest all land here."""
    write_table(tmp_path, TABLE.split("$"), pa.table({"id": [1]}))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"{SECRET} at {LOCATION}")

    monkeypatch.setattr(pages_ep.lance, "dataset", _boom)
    state = AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)}))

    with pytest.raises(ServiceUnavailableError) as caught:
        pages_ep._open(state, TABLE, "tok")  # noqa: SLF001
    message = str(caught.value)
    assert not isinstance(caught.value, NotFoundError)
    assert SECRET not in message, f"the detail carried the driver text: {message}"
    assert LOCATION not in message, f"the detail carried the object-store location: {message}"


def test_a_table_the_catalog_does_not_hold_stays_a_404(tmp_path: Path) -> None:
    """Absence must not become 503: that would make a missing registration look retryable and hide it behind an outage
    alert."""
    write_table(tmp_path, ["acme", "bronze", "other"], pa.table({"id": [1]}))
    state = AppState(settings=Settings.model_validate({"MEDIA_CATALOG_DIR": str(tmp_path)}))

    with pytest.raises(NotFoundError):
        pages_ep._open(state, TABLE, "tok")  # noqa: SLF001
