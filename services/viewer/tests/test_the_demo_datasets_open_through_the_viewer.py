"""The local stack's demo datasets open through the real viewer, each as the player reads it.

`scripts/seed_demo_corpus.py` is what a person runs before they open the app (`make local`), so what it writes is held
to what the viewer serves: seeded into a directory catalog under ``tmp_path`` and read back through the viewer app over
HTTP, the three datasets list, the page corpus signs and serves its frame, its cover and its thumbnail and answers its
topic tree and its knowledge graph (an entity's clips and neighbours), the recordings serve their WAV, their timed
segments with word alignments, their speaker turns and their waveform, and the films serve their WebM, their timed shots
and the waveform of their sound; a page's picture answers that it holds no sound. The demo's plain tables answer the set-ups the app's browser suite draws
exactly as that suite's recording says (`tests.derived_recording`), every node named by rows it lists.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from urllib.parse import parse_qs

import lance_namespace
import lancedb
from fastapi.testclient import TestClient

from scripts.seed_demo_corpus import seed
from tests.derived_recording import record, recorded
from tests.media_catalog import lake, lakehouse_dir
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app


def _signed(client: TestClient, dataset: str, item: dict[str, object]) -> tuple[int | None, str, bytes]:
    """The one item's signed URL, fetched: its status, type and bytes; the viewer's reason in place of the type when it
    signed none."""
    [url] = client.post("/api/signed-media", json={"dataset": dataset, "items": [item]}).json()["urls"]
    if not url["url"]:
        return None, str(url["detail"]), b""
    served = client.get(str(url["url"]))
    return served.status_code, served.headers["content-type"], served.content


def test_the_seeded_pages_recordings_and_films_open_as_the_player_reads_them(tmp_path: Path) -> None:
    properties = {"root": str(tmp_path / "catalog")}
    pages, recordings, films, letters = (
        lake(d) for d in seed(lance_namespace.connect("dir", properties), lancedb.connect_namespace("dir", properties), "demo")
    )
    key = tmp_path / "media-url-key"
    key.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    settings = ViewerSettings.model_validate({**lakehouse_dir(properties["root"]), "VIEWER_MEDIA_URL_KEY_FILE": str(key)})
    app = create_viewer_app(settings)
    app.dependency_overrides[get_viewer_settings] = lambda: settings

    with TestClient(app) as client:
        listed = sorted(d["id"] for source in client.get("/api/datasets").json()["sources"] for d in source["datasets"])
        described = {d: client.get(f"/api/datasets/{d}/descriptor") for d in (pages, recordings, films, letters)}
        frame = _signed(client, pages, {"role": "frame", "doc_id": "fe00cd746463ad2c", "keys": [1, 1], "frame_idx": 0})
        cover = _signed(client, pages, {"role": "media", "doc_id": "fe00cd746463ad2c"})
        thumb = _signed(client, pages, {"role": "thumbnail", "doc_id": "fe00cd746463ad2c"})
        audio = _signed(client, recordings, {"role": "media", "doc_id": "c41f0e2a9b7d3e15"})
        chunks = client.get("/api/doc-chunks/c41f0e2a9b7d3e15", params={"dataset": recordings}).json()["chunks"]
        turns = client.get("/api/diarization/c41f0e2a9b7d3e15", params={"dataset": recordings}).json()
        sound = client.get(f"/api/datasets/{recordings}/documents/c41f0e2a9b7d3e15/peaks").json()
        video = _signed(client, films, {"role": "media", "doc_id": "f1a07c3e9d2b4e68"})
        shots = client.get("/api/doc-chunks/f1a07c3e9d2b4e68", params={"dataset": films}).json()["chunks"]
        soundtrack = client.get(f"/api/datasets/{films}/documents/f1a07c3e9d2b4e68/peaks").json()
        silent_page = client.get(f"/api/datasets/{pages}/documents/fe00cd746463ad2c/peaks")
        topics = client.get("/api/topics", params={"dataset": pages}).json()
        graph = client.get("/api/graph/status", params={"dataset": pages}).json()
        [archive] = client.get("/api/graph/search", params={"dataset": pages, "q": "riksarkiv"}).json()["matches"]
        entity = client.get(f"/api/graph/entity/{archive['entity_id']}", params={"dataset": pages}).json()
        paragraphs = [client.get("/api/rows", params={"dataset": letters, "page": page, "per_page": 2}).json() for page in (1, 2)]
        no_documents = client.get("/api/documents", params={"dataset": letters}).json()

    assert listed == ["lake~demo$corpus", "lake~demo$films", "lake~demo$letters", "lake~demo$recordings"]
    kinds = {d: (r.status_code, (r.json().get("declared", {}).get("document") or {}).get("kind")) for d, r in described.items()}
    # The letters declare no document: an item of theirs is its text.
    assert kinds == {pages: (200, "image"), recordings: (200, "audio"), films: (200, "video"), letters: (200, None)}
    for status, media_type, body in (frame, cover, thumb):
        assert (status, media_type, body[:8]) == (200, "image/png", b"\x89PNG\r\n\x1a\n")
    assert (audio[0], audio[1], audio[2][:4], audio[2][8:12]) == (200, "audio/wav", b"RIFF", b"WAVE")
    assert [(c["chunk_id"], c["start"], c["end"], len(c["alignments"])) for c in chunks] == [(0, 0.0, 2.0, 1), (1, 2.0, 4.0, 1), (2, 4.0, 6.0, 1)]
    assert (turns["built"], turns["speakers"]) == (True, ["Speaker A", "Speaker B"])
    # Six seconds at a hundred buckets a second, each a min and a max: the voices' envelope, never louder than full scale.
    heard = base64.b64decode(sound["peaks"])
    assert (sound["duration"], sound["decoded"], sound["rate"], sound["audio"], sound["cut"], len(heard)) == (6.0, 6.0, 100.0, True, None, 1200)
    assert max(int.from_bytes(heard[i : i + 1], signed=True) for i in range(1, len(heard), 2)) > 30
    assert (video[0], video[1], video[2][:4]) == (200, "video/webm", b"\x1aE\xdf\xa3")
    assert [(c["chunk_id"], c["start"], c["end"]) for c in shots] == [(0, 0.0, 3.0), (1, 3.0, 6.0)]
    assert (round(soundtrack["duration"]), soundtrack["decoded"], soundtrack["audio"], soundtrack["cut"]) == (6, 6.0, True, None)
    assert (silent_page.status_code, silent_page.json()["audio"], silent_page.json()["peaks"]) == (200, False, "")
    assert (topics["built"], topics["layers"], topics["n_chunks"]) == (True, 2, 10)
    assert [t["name"] for t in topics["hierarchy"]["children"]] == ["Förvaltning", "Rättskipning", "Sjöfart"]
    assert (graph["built"], graph["entities"], graph["relations"], graph["videos"]) == (True, 10, 9, 3)
    assert [(c["doc_id"], c["title"]) for c in entity["clips"]] == [
        ("b2c8e5417d90a6f4", "Domboksregister 1688"),
        ("fe00cd746463ad2c", "Landskapshandlingar 1663"),
    ]
    assert sorted((n["name"], n["direction"]) for n in entity["neighbors"]) == [("Domboken", "out"), ("Landshövdingen", "in"), ("Protokoll", "out")]
    # The letters hold no document to list: an item of theirs is a paragraph, and their rows are browsed as hits.
    assert no_documents["total"] == 0
    assert [(p["total"], [(r["doc_id"], r["chunk_id"], r["text"][:24]) for r in p["items"]]) for p in paragraphs] == [
        (3, [("a7e3c19d04b25f86", 0, "Landshövdingen Erik Spar"), ("a7e3c19d04b25f86", 1, "Han ber att tullen i Kal")]),
        (3, [("5d0b8f2e6c1a9437", 0, "Brukspatron Anna Lind me")]),
    ]


def test_the_plain_tables_answer_the_set_ups_the_browser_suite_draws_as_its_recording_says(tmp_path: Path) -> None:
    answers = {key: answer["json"] for key, answer in record(tmp_path).items() if "json" in answer}
    pages = {(key.split("?")[0], parse_qs(key.split("?")[1])["key"][0]): page for key, page in answers.items() if "/rows?" in key and "key=" in key}
    # Each drawn node lists rows: its count, its first row and the rows its key lists are the same rows, a node only
    # pointed at (a port no cargo leaves, a scan cited and not held) as much as any.
    listed = {
        node["id"]: (node["rows"], page["total"], node["first"] in [row["rowid"] for row in page["rows"]])
        for key, graph in answers.items()
        if key.split("?")[0].endswith("/graph")
        for node in graph["nodes"]
        for page in [pages[(f"{key.split('?')[0]}/rows", node["id"])]]
    }

    assert {node: (rows == total, first) for node, (rows, total, first) in listed.items()} == dict.fromkeys(listed, (True, True))
    assert listed["s:Lübeck"] == (12, 12, True)
    said = {key: answer["json"] for key, answer in recorded().items() if "json" in answer}
    stale = "the recording is not what the viewer answers: uv run python -m scripts.record_derived_answers"
    assert {key: _beside_its_waveform(body) for key, body in answers.items()} == {key: _beside_its_waveform(body) for key, body in said.items()}, stale
    # A waveform is the levels a decoder heard. A film's sound is a lossy codec's, whose decoded samples differ in the
    # last step between processor architectures (measured 2026-11-20: the film's levels recorded on aarch64 are not
    # byte-equal on x86-64), so its levels are held to one step of 127 and its length exactly.
    for key in said:
        heard, kept = _levels(answers[key]), _levels(said[key])
        assert len(heard) == len(kept), f"{key}: {stale}"
        assert max((abs(a - b) for a, b in zip(heard, kept, strict=True)), default=0) <= 1, f"{key}: {stale}"


def _beside_its_waveform(body: object) -> object:
    """An answer without its waveform's levels, which :func:`_levels` reads."""
    return {name: value for name, value in body.items() if name != "peaks"} if isinstance(body, dict) and "peaks" in body else body


def _levels(body: object) -> list[int]:
    """The signed levels of an answer's waveform, none where the answer carries no waveform."""
    packed = body.get("peaks") if isinstance(body, dict) else None
    return [level - 256 if level > 127 else level for level in base64.b64decode(packed)] if isinstance(packed, str) else []
