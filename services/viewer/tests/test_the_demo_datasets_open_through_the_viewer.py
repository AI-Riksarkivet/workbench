"""The local stack's demo datasets open through the real viewer, each as the player reads it.

`scripts/seed_demo_corpus.py` is what a person runs before they open the app (`make local`), so what it writes is held
to what the viewer serves: seeded into a directory catalog under ``tmp_path`` and read back through the viewer app over
HTTP, both datasets list, the page corpus signs and serves its frame, its cover and its thumbnail and answers its topic
tree and its knowledge graph (an entity's clips and neighbours), and the recordings serve their WAV, their timed segments
with word alignments, and their speaker turns.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

import lance_namespace
import lancedb
from fastapi.testclient import TestClient

from scripts.seed_demo_corpus import seed
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


def test_the_seeded_pages_and_recordings_open_as_the_player_reads_them(tmp_path: Path) -> None:
    properties = {"root": str(tmp_path / "catalog")}
    pages, recordings = (lake(d) for d in seed(lance_namespace.connect("dir", properties), lancedb.connect_namespace("dir", properties), "demo"))
    key = tmp_path / "media-url-key"
    key.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    settings = ViewerSettings.model_validate({**lakehouse_dir(properties["root"]), "VIEWER_MEDIA_URL_KEY_FILE": str(key)})
    app = create_viewer_app(settings)
    app.dependency_overrides[get_viewer_settings] = lambda: settings

    with TestClient(app) as client:
        listed = sorted(d["id"] for source in client.get("/api/datasets").json()["sources"] for d in source["datasets"])
        described = {d: client.get(f"/api/datasets/{d}/descriptor") for d in (pages, recordings)}
        frame = _signed(client, pages, {"role": "frame", "doc_id": "fe00cd746463ad2c", "keys": [1, 1], "frame_idx": 0})
        cover = _signed(client, pages, {"role": "media", "doc_id": "fe00cd746463ad2c"})
        thumb = _signed(client, pages, {"role": "thumbnail", "doc_id": "fe00cd746463ad2c"})
        audio = _signed(client, recordings, {"role": "media", "doc_id": "c41f0e2a9b7d3e15"})
        chunks = client.get("/api/doc-chunks/c41f0e2a9b7d3e15", params={"dataset": recordings}).json()["chunks"]
        turns = client.get("/api/diarization/c41f0e2a9b7d3e15", params={"dataset": recordings}).json()
        topics = client.get("/api/topics", params={"dataset": pages}).json()
        graph = client.get("/api/graph/status", params={"dataset": pages}).json()
        [archive] = client.get("/api/graph/search", params={"dataset": pages, "q": "riksarkiv"}).json()["matches"]
        entity = client.get(f"/api/graph/entity/{archive['entity_id']}", params={"dataset": pages}).json()

    assert listed == ["lake~demo$corpus", "lake~demo$recordings"]
    kinds = {d: (r.status_code, r.json().get("declared", {}).get("document", {}).get("kind")) for d, r in described.items()}
    assert kinds == {pages: (200, "image"), recordings: (200, "audio")}
    for status, media_type, body in (frame, cover, thumb):
        assert (status, media_type, body[:8]) == (200, "image/png", b"\x89PNG\r\n\x1a\n")
    assert (audio[0], audio[1], audio[2][:4], audio[2][8:12]) == (200, "audio/wav", b"RIFF", b"WAVE")
    assert [(c["chunk_id"], c["start"], c["end"], len(c["alignments"])) for c in chunks] == [(0, 0.0, 2.0, 1), (1, 2.0, 4.0, 1), (2, 4.0, 6.0, 1)]
    assert (turns["built"], turns["speakers"]) == (True, ["Speaker A", "Speaker B"])
    assert (topics["built"], topics["layers"], topics["n_chunks"]) == (True, 2, 10)
    assert [t["name"] for t in topics["hierarchy"]["children"]] == ["Förvaltning", "Rättskipning", "Sjöfart"]
    assert (graph["built"], graph["entities"], graph["relations"], graph["videos"]) == (True, 10, 9, 3)
    assert [(c["doc_id"], c["title"]) for c in entity["clips"]] == [
        ("b2c8e5417d90a6f4", "Domboksregister 1688"),
        ("fe00cd746463ad2c", "Landskapshandlingar 1663"),
    ]
    assert sorted((n["name"], n["direction"]) for n in entity["neighbors"]) == [("Domboken", "out"), ("Landshövdingen", "in"), ("Protokoll", "out")]
