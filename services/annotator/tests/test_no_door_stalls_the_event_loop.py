"""No annotator door freezes its event loop with blocking work.

The annotator process has one event loop, so a blocking call made on it freezes every other in-flight request and the
`/livez` + `/readyz` probes for as long as it takes. The blocking calls the doors make: an import decodes its whole
request body (`shapes_from_ipc`: the Arrow decode, `to_pylist()`, per-row Pydantic construction and the ontology pass);
the canvas's doors (read, save, revert, tags) resolve their dataset through the catalog (`annotator.corpus.resolve`:
catalog requests and a manifest read per table); and the read builds and encodes a draft of up to 10,000 shapes as
Arrow. Each such call here waits for a coroutine on the app's own loop (`client.portal`, where the state store's pool
lives) to release it, so it is released only when it runs off the loop; no wall clock is read.
"""

from __future__ import annotations

import asyncio
import threading
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import annotator.annotations.wire as wire_module
import annotator.corpus as corpus_module
from annotator.api.v1.endpoints import tasks as tasks_ep
from services.annotator.tests.conftest import WORK_DOC, WORK_QUERY, WORK_UNIT


#: Each door and the blocking call it makes, as (method, path, body, module, the call's name there). A body of bytes is
#: sent as it is, any other as JSON.
_DOORS = [
    pytest.param("POST", "/tasks/t1/import", b"arrow-ipc-bytes", tasks_ep, "shapes_from_ipc", id="import"),
    pytest.param("GET", WORK_UNIT, None, corpus_module, "dataset_handle", id="read-resolve"),
    pytest.param("GET", WORK_UNIT, None, wire_module, "wire_table", id="read-encode"),
    pytest.param("POST", WORK_UNIT, {"base_version": 0}, corpus_module, "dataset_handle", id="save"),
    pytest.param("POST", f"{WORK_UNIT}/revert", {"revision": 1}, corpus_module, "dataset_handle", id="revert"),
    pytest.param(
        "POST", "/api/annotations/tags", {"adds": [{"doc_id": WORK_DOC, "keys": [0, 19], "labels": ["speech"]}]}, corpus_module, "dataset_handle", id="tags"
    ),
]


@pytest.mark.parametrize(("method", "path", "body", "module", "name"), _DOORS)
def test_a_door_makes_its_blocking_calls_off_the_loop(
    work_client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: bytes | dict[str, Any] | None, module: ModuleType, name: str
) -> None:
    released: list[bool] = []
    gate = threading.Event()
    blocking = getattr(module, name)

    def _slow(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - stands in for whichever call it gates
        """Stands in for a slow catalog, a large body or a large draft: it goes on once the loop has run the releasing
        coroutine."""
        released.append(gate.wait(timeout=5))
        return blocking(*args, **kwargs)

    monkeypatch.setattr(module, name, _slow)
    app, portal = work_client.app, work_client.portal
    assert portal is not None
    sent: dict[str, Any] = {"content": body} if isinstance(body, bytes) else {"json": body}

    async def _race() -> httpx.Response:
        async def _release() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ann") as http:
            response, _ = await asyncio.gather(http.request(method, path, params=WORK_QUERY, **sent), _release())
            return response

    response = portal.call(_race)

    assert released == [True], f"{method} {path} called {name} ON the event loop ({response.status_code}: {response.text})"
