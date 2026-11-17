"""What the real viewer answers for the set-ups the app's browser suite draws, on the demo's own plain tables.

The app's Playwright suite runs against mock services (`frontend/app/e2e/mock-services.ts`), and a mock answers what
its author believed the viewer answers. For the set-ups a person draws on the demo's plain tables (the field survey's
observations and the bare scans table, `scripts/seed_demo_corpus.py`), the mock serves instead the viewer's own answers,
recorded here: the dataset's facts, its first page of rows, the profile, each set-up's drawn view and its check, and the
rows of every node drawn. For the labeling canvas over a recording and a video, the demo's stamped recordings and films
(`seed`): one recording's and one film's descriptor, parts, waveform (`viewer.services.peaks`) and the film's own WebM
bytes, as the signed link serves them (`{"bytes": <base64>}`), so the canvas decodes and draws a real video. `scripts/record_derived_answers.py` writes them to :data:`RECORDING`, and the viewer's suite
holds that file to what the viewer answers now, so the browser draws what the server sends.

Each answer is keyed by its path and its query with the parameters sorted (:func:`answer_key`), as the mock looks it up.
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import lance_namespace
import lancedb
from fastapi.testclient import TestClient

from scripts.seed_demo_corpus import ARCHIVE_NAMESPACE, OBSERVATIONS, SCANS, seed, seed_archive, seed_loose
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app


#: Where the recording lives: beside the mock that serves it.
RECORDING = Path(__file__).resolve().parents[1] / "frontend" / "app" / "e2e" / "seeded-answers.json"

#: The two datasets, by the ids the recording's sources give them.
SURVEY = f"seeded~{'$'.join(ARCHIVE_NAMESPACE)}"
LOOSE = "loose~"

#: The demo's recording and film the canvas specs label, by dataset and document.
TIMED: tuple[tuple[str, str], ...] = (("lake~demo$recordings", "c41f0e2a9b7d3e15"), ("lake~demo$films", "f1a07c3e9d2b4e68"))

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
    lakehouse = {"root": str(root / "lake")}
    seed(lance_namespace.connect("dir", lakehouse), lancedb.connect_namespace("dir", lakehouse), "demo")
    key = root / "media-url-key"
    key.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    settings = ViewerSettings.model_validate(
        {
            "VIEWER_MEDIA_URL_KEY_FILE": str(key),
            "WORKBENCH_SOURCES": [
                {"kind": "namespace", "name": "seeded", "impl": "dir", "properties": {"root": str(root / "archive")}},
                {"kind": "tables", "name": "loose", "uri": scans},
                {"kind": "rask", "name": "lake", "dir": lakehouse["root"], "identity": "none"},
            ],
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
        for (dataset, doc), keys in zip(TIMED, ([0, 1], [0, 0]), strict=True):
            # The task's own row, as the by-key door answers it: keyed by the unit, since the door is a POST.
            rows = client.post("/api/atlas/chunks/by-key", params={"dataset": dataset}, json={"keys": [{"doc_id": doc, "keys": keys}]})
            assert rows.status_code == 200, f"by-key {dataset}: {rows.status_code}"
            answers[answer_key("/api/atlas/chunks/by-key", {"dataset": dataset, "unit": "/".join([doc, *map(str, keys)])})] = {"json": rows.json()}
            ask(f"/api/datasets/{dataset}/descriptor", {})
            ask(f"/api/doc-chunks/{doc}", {"dataset": dataset})
            ask(f"/api/diarization/{doc}", {"dataset": dataset})
            ask(f"/api/datasets/{dataset}/documents/{doc}/peaks", {})
        [link] = client.post("/api/signed-media", json={"dataset": TIMED[1][0], "items": [{"role": "media", "doc_id": TIMED[1][1]}]}).json()["urls"]
        served = client.get(str(link["url"]))
        assert served.status_code == 200, f"the film: {served.status_code}"
        answers[f"/__media/{TIMED[1][1]}"] = {"bytes": base64.b64encode(served.content).decode()}
    return answers


def recorded() -> dict[str, dict[str, Any]]:
    """The recording as the browser suite's mock serves it."""
    return json.loads(RECORDING.read_text())


def write(answers: dict[str, dict[str, Any]]) -> None:
    """Write ``answers`` as the recording: one answer per line, so a change to one reads as one line of a diff."""
    lines = (json.dumps({key: answers[key]}, ensure_ascii=False, sort_keys=True)[1:-1] for key in sorted(answers))
    RECORDING.write_text("{\n" + ",\n".join(lines) + "\n}\n")
