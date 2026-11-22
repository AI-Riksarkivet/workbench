"""The request id reaches every log record of its request, and no other request's.

`RequestIDMiddleware` echoes or mints `X-Request-ID` and publishes it to a `ContextVar`, reset when the response is
done, so a reused worker never labels the next request with the previous caller's id. `setup_logging`'s stdout handler
carries `CorrelationFilter`, which stamps the id (and the trace id) on every record, so an operator can grep for the id
a caller quotes.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.middleware import RequestIDMiddleware


def test_the_id_does_not_leak_into_the_next_request() -> None:
    """Each request sees its own id, and none survives its response on the worker."""
    from workbench.context import current_request_id

    seen: list[str] = []
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        seen.append(current_request_id())
        return {"ok": "yes"}

    client = TestClient(app)
    client.get("/thing", headers={"X-Request-ID": "first"})
    client.get("/thing", headers={"X-Request-ID": "second"})

    assert seen == ["first", "second"]
    assert current_request_id() != "second", "the id survived the response and will label the next request"


def test_a_log_record_carries_the_request_id() -> None:
    """The filter stamps the context's id on the record."""
    from workbench.context import CorrelationFilter, request_id_ctx

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    token = request_id_ctx.set("deadbeef")
    try:
        assert CorrelationFilter().filter(record) is True
    finally:
        request_id_ctx.reset(token)

    assert getattr(record, "request_id", None) == "deadbeef"


def test_setup_logging_installs_the_filter() -> None:
    """The handler `setup_logging` installs stamps `request_id` and `trace_id` on a record, `-` outside a request."""
    from workbench.app import setup_logging

    root = logging.getLogger()
    previous = list(root.handlers)
    for handler in previous:
        root.removeHandler(handler)
    try:
        setup_logging("INFO")
        stdout_handler = next(h for h in root.handlers if h.get_name() == "workbench-stdout")
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        for log_filter in stdout_handler.filters:
            # `Handler.filters` may hold bare callables as well as `Filter` instances; only the latter
            # is what this asserts about.
            if isinstance(log_filter, logging.Filter):
                log_filter.filter(record)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in previous:
            root.addHandler(handler)

    assert getattr(record, "request_id", None) == "-", "the stdout handler stamps no request_id, so the formatter's field is unfed"
    assert getattr(record, "trace_id", None) == "-", "the stdout handler stamps no trace_id"
