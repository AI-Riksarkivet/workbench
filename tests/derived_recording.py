"""What the real viewer answers for the set-ups the app's browser suite draws, on the demo's own plain tables.

The app's Playwright suite runs against mock services (`frontend/app/e2e/mock-services.ts`), and a mock answers what
its author believed the viewer answers. For the set-ups a person draws on the demo's plain tables (the field survey's
observations and the bare scans table, `scripts/seed_demo_corpus.py`), the mock serves instead the viewer's own answers,
recorded here: the dataset's facts, its first page of rows, the profile, each set-up's drawn view and its check, and the
rows of every node drawn. `scripts/record_derived_answers.py` writes them to :data:`RECORDING`, and the viewer's suite
holds that file to what the viewer answers now, so the browser draws what the server sends.

Each answer is keyed by its path and its query with the parameters sorted (:func:`answer_key`), as the mock looks it up.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient

from scripts.seed_demo_corpus import ARCHIVE_NAMESPACE, OBSERVATIONS, SCANS, seed_archive, seed_loose
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app


#: Where the recording lives: beside the mock that serves it.
RECORDING = Path(__file__).resolve().parents[1] / "frontend" / "app" / "e2e" / "seeded-answers.json"

#: The two datasets, by the ids the recording's sources give them.
SURVEY = f"seeded~{'$'.join(ARCHIVE_NAMESPACE)}"
LOOSE = "loose~"

#: The set-ups the browser suite draws: what the forms suggest on each table, as the person confirms them.
SETUPS: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    (SURVEY, OBSERVATIONS): {
        "atlas": {"vector": "embedding", "label": "note"},
        "graph": {"shape": "edges", "source": "shipped_from", "target": "shipped_to"},
        "tree": {"shape": "path", "node": "place", "separator": "/"},
    },
    (LOOSE, SCANS): {
        "atlas": {"vector": "embedding", "label": "scan"},
        "graph": {"shape": "self", "source": "scan", "target": "cites", "label": "scan"},
        "tree": {"shape": "parent", "node": "scan", "parent": "cites"},
    },
}


def answer_key(path: str, params: dict[str, str]) -> str:
    """An answer's key: its path, and its query with the parameters sorted."""
    return f"{path}?{urlencode(sorted(params.items()))}" if params else path


def _tree_keys(node: dict[str, object]) -> list[str]:
    keys: list[str] = []
    stack = [node]
    while stack:
        here = stack.pop()
        if isinstance(here.get("id"), str):
            keys.append(str(here["id"]))
        children = here.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return keys


def record(root: Path) -> dict[str, dict[str, Any]]:
    """The demo's plain tables seeded under ``root``, and every answer the browser suite asks of them, from the viewer.

    Raises:
        AssertionError: The viewer answered one of them with anything but 200.
    """
    seed_archive(str(root / "archive"))
    scans = seed_loose(str(root / "loose"))
    settings = ViewerSettings.model_validate(
        {
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "seeded", "impl": "dir", "properties": {"root": str(root / "archive")}},
                {"kind": "tables", "name": "loose", "uri": scans},
            ]
        }
    )
    app = create_viewer_app(settings)
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    answers: dict[str, dict[str, Any]] = {}
    with TestClient(app) as client:

        def ask(path: str, params: dict[str, str]) -> Any:
            answer = client.get(path, params=params)
            assert answer.status_code == 200, f"{path} {params}: {answer.status_code} {answer.text[:300]}"
            if answer.headers["content-type"].startswith("application/json"):
                body = answer.json()
                answers[answer_key(path, params)] = {"json": body}
                return body
            answers[answer_key(path, params)] = {"arrow": base64.b64encode(answer.content).decode()}
            return None

        for (dataset, table), setups in SETUPS.items():
            base = f"/api/datasets/{quote(dataset, safe='~')}"
            facts = ask(base, {})
            tables = facts["tables"] if facts is not None else None
            assert isinstance(tables, dict), f"{dataset}: no tables in its facts"
            version = str(tables[table]["version"])
            here = f"{base}/tables/{table}"
            ask(f"{here}/rows", {"offset": "0", "limit": "50"})
            ask(f"{here}/profile", {"version": version})
            for read, setup in setups.items():
                params = {**setup, "version": version}
                drawn = ask(f"{here}/{read}", params)
                if read == "atlas" or drawn is None:
                    continue
                ask(f"{here}/{read}/check", params)
                nodes, hierarchy = drawn.get("nodes"), drawn.get("hierarchy")
                keys = [str(n["id"]) for n in nodes] if isinstance(nodes, list) else _tree_keys(hierarchy) if isinstance(hierarchy, dict) else []
                for key in keys:
                    ask(f"{here}/{read}/rows", {**params, "key": key, "offset": "0", "limit": "50"})
    return answers


def recorded() -> dict[str, dict[str, Any]]:
    """The recording as the browser suite's mock serves it."""
    return json.loads(RECORDING.read_text())


def write(answers: dict[str, dict[str, Any]]) -> None:
    """Write ``answers`` as the recording: one answer per line, so a change to one reads as one line of a diff."""
    lines = (json.dumps({key: answers[key]}, ensure_ascii=False, sort_keys=True)[1:-1] for key in sorted(answers))
    RECORDING.write_text("{\n" + ",\n".join(lines) + "\n}\n")
