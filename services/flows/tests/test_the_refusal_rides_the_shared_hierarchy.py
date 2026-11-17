"""`create_run` refuses through the estate's exception hierarchy, not around it.

`create_run` was declared `-> RunState | JSONResponse` and hand-built its own problem+json body,
for one stated reason: `service_kit.exceptions._problem` flattened a problem to four keys, and the
refusal's whole point is a structured `problems` LIST the builder highlights nodes from
(FLOWS-422-BYPASSES-HIERARCHY). So the one route in this service that can refuse for a domain reason
was also the one route whose refusal no handler produced — a second error plane, one route wide.

The cost is not theoretical. A union return type means the next refusal added here can return any
`JSONResponse` at all with nothing checking its shape, and the hand-built body is a private copy of a
document the estate owns: `_problem` gaining a member (or changing one) leaves this route answering
the old shape, silently, forever.

The hierarchy now carries RFC 9457 extension members (`packages/service-kit/tests/
test_problem_extension_members.py`), so the reason is gone and so is the escape hatch. The refusal
keeps its specific `about:blank#flow-invalid` type — a domain naming its own failure is the point of
the taxonomy, not a departure from it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


# `client` is the real flows app (`conftest.py`), whose factory mounts `register_handlers`: the refusal must be PRODUCED
# by that handler rather than assembled beside it.


def test_the_refusal_body_is_the_shared_builders_output(client: TestClient) -> None:
    """Five members, produced by the one handler every other refusal in this service goes through."""
    response = client.post(
        "/api/flows/projects/p1/runs",
        json={"graph": {"nodes": [{"id": "a", "kind": "quantum"}], "edges": []}, "seeds": {}},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank#flow-invalid",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": "1 problem(s) in the graph",
        "problems": ["unknown node kind: quantum (node a)"],
    }
