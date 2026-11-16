"""A signed media URL serves the one cell it was signed for, with Range, and nothing once it is altered or expired.

The person asks the signing door for a batch of URLs; a media element fetches each with no token, and the viewer reads
the cell as its own identity, so a replica that never saw the person serves it. Driven through the real viewer app over
real tables on the ``dir`` catalog (and a REST catalog that answers per bearer, ``tests.vending_catalog``), the key in a
file as a mounted secret is.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance import blob_array, blob_field

from service_kit.governed.token_exchange import LakehouseCaller, LakehouseTokens
from tests.media_catalog import lakehouse_dir, lakehouse_rest, write_table
from tests.vending_catalog import VendingCatalog
from viewer.api import security
from viewer.core.config import ViewerSettings, get_viewer_settings
from viewer.main import create_viewer_app
from viewer.services import signed_media


_DOCUMENTS = {"identity": {"key_fields": ["doc_id"]}, "document": {"table": "docs", "media_blob": "media", "mime": "mime"}}
#: Above blob v2's 64 KiB inline threshold, so the payload is a byte range in a packed sidecar.
_PAYLOADS = {"d1": bytes(range(256)) * 800, "d2": b"\x00\x01" * 70_000}
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x2a" * 300


def _docs() -> pa.Table:
    schema = pa.schema([pa.field("doc_id", pa.string()), blob_field("media"), pa.field("mime", pa.string())])
    return pa.table({"doc_id": list(_PAYLOADS), "media": blob_array(list(_PAYLOADS.values())), "mime": ["video/mp4", "audio/wav"]}, schema=schema)


def _app(root: Path, catalog: dict[str, object] | None = None) -> FastAPI:
    key_file = root / "media-url-key"
    if not key_file.exists():
        key_file.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    settings = ViewerSettings.model_validate({**(catalog or {**lakehouse_dir(str(root))}), "VIEWER_MEDIA_URL_KEY_FILE": str(key_file)})
    app = create_viewer_app(settings)
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    return app


def _sign(client: TestClient, items: list[dict[str, object]], dataset: str | None = "lake~wh$corpus") -> list[dict[str, object]]:
    answer = client.post("/api/signed-media", json={"dataset": dataset, "items": items})
    assert answer.status_code == 200, answer.text
    return answer.json()["urls"]


def _urls(client: TestClient) -> dict[str, str]:
    urls = _sign(client, [{"role": "media", "doc_id": doc} for doc in _PAYLOADS])
    return {doc: str(url["url"]) for doc, url in zip(_PAYLOADS, urls, strict=True)}


def test_a_signed_url_streams_its_blob_with_range(tmp_path: Path) -> None:
    write_table(tmp_path, ["wh", "corpus", "docs"], _docs(), descriptor=_DOCUMENTS)
    with TestClient(_app(tmp_path)) as client:
        url = _urls(client)["d1"]
        ranged = client.get(url, headers={"Range": "bytes=100-399"})
        whole = client.get(url)

    assert ranged.status_code == 206, ranged.text
    assert ranged.content == _PAYLOADS["d1"][100:400]
    assert ranged.headers["content-range"] == f"bytes 100-399/{len(_PAYLOADS['d1'])}"
    assert (whole.status_code, whole.content, whole.headers["content-type"]) == (200, _PAYLOADS["d1"], "video/mp4")
    assert whole.headers["etag"] == ranged.headers["etag"]
    assert whole.headers["cache-control"].startswith("public, max-age=")
    assert whole.headers["cache-control"].endswith(", immutable")


def _another_objects_payload(urls: dict[str, str]) -> str:
    payload_of_d2 = urls["d2"].rsplit("/", 1)[1].split(".")[0]
    prefix, signature_of_d1 = urls["d1"].rsplit(".", 1)
    return f"{prefix.rsplit('/', 1)[0]}/{payload_of_d2}.{signature_of_d1}"


def _tampered_signature(urls: dict[str, str]) -> str:
    url = urls["d1"]
    return url[:-2] + ("AA" if not url.endswith("AA") else "BB")


@pytest.mark.parametrize(
    ("alter", "expires"),
    [
        pytest.param(_another_objects_payload, False, id="another-object"),
        pytest.param(_tampered_signature, False, id="tampered-signature"),
        pytest.param(lambda urls: urls["d1"], True, id="expired"),
    ],
)
def test_an_altered_or_expired_url_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alter: Callable[[dict[str, str]], str], expires: bool) -> None:
    write_table(tmp_path, ["wh", "corpus", "docs"], _docs(), descriptor=_DOCUMENTS)
    with TestClient(_app(tmp_path)) as client:
        url = alter(_urls(client))
        if expires:
            later = signed_media.now() + 3600
            monkeypatch.setattr(signed_media, "now", lambda: later)
        refused = client.get(url)

    assert refused.status_code == 403, refused.text
    assert refused.headers["content-type"] == "application/problem+json"


class _ServiceOnly:
    """The viewer's confidential client on a replica that never saw the person: its own token, and no exchange to make."""

    def for_service(self) -> str:
        return "service-token"

    def for_person(self, subject: str) -> str:
        raise AssertionError(f"a signed GET asked for {subject}'s token")


def test_a_replica_that_never_saw_the_person_serves_the_url_as_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = {**_DOCUMENTS, "document": {**_DOCUMENTS["document"], "table": "chunks"}}
    location = write_table(tmp_path, ["wh", "corpus", "chunks"], _docs(), descriptor=descriptor).uri
    catalog = VendingCatalog(location, {"person-token": {}, "service-token": {}})
    signer, other_replica = (_app(tmp_path, lakehouse_rest(catalog.url)) for _ in range(2))
    signer.dependency_overrides[security._deps.lakehouse_caller] = lambda: LakehouseCaller(subject="keycloak~ada", token=lambda: "person-token")
    other_replica.state.lakehouse_tokens = cast("LakehouseTokens", _ServiceOnly())
    try:
        with TestClient(signer) as client:
            url = str(_sign(client, [{"role": "media", "doc_id": "d1"}])[0]["url"])
        signed_calls = len(catalog.calls)
        with TestClient(other_replica) as client:
            served = client.get(url)
    finally:
        catalog.close()

    assert (served.status_code, served.content) == (200, _PAYLOADS["d1"]), served.text
    assert {bearer for _op, bearer, _trace in catalog.calls[signed_calls:]} == {"Bearer service-token"}


def test_a_chunks_frame_is_served_by_its_door_and_by_a_signed_url(tmp_path: Path) -> None:
    descriptor = {"identity": {"key_fields": ["doc_id", "group_id", "chunk_id"]}, "capabilities": {"frames": "frames.frame"}}
    schema = pa.schema([("doc_id", pa.string()), ("group_id", pa.int64()), ("chunk_id", pa.int64()), ("frame_idx", pa.int64()), blob_field("frame")])
    frames = pa.table(
        {"doc_id": ["d1", "d1"], "group_id": [0, 0], "chunk_id": [1, 1], "frame_idx": [0, 1], "frame": blob_array([b"first", _JPEG])}, schema=schema
    )
    write_table(tmp_path, ["wh", "corpus", "frames"], frames, descriptor=descriptor)

    with TestClient(_app(tmp_path)) as client:
        by_door = client.get("/api/chunk-frame/d1/0/1", params={"dataset": "lake~wh$corpus", "frame_idx": 1})
        url = _sign(client, [{"role": "frame", "doc_id": "d1", "keys": [0, 1], "frame_idx": 1}])[0]["url"]
        by_url = client.get(str(url))

    assert (by_door.status_code, by_door.content, by_door.headers["x-content-type-options"]) == (200, _JPEG, "nosniff"), by_door.text
    assert (by_url.status_code, by_url.content, by_url.headers["content-type"]) == (200, _JPEG, "image/jpeg")


@pytest.mark.parametrize("blob", [pytest.param(True, id="blob-v2"), pytest.param(False, id="plain-binary")])
def test_a_pages_payload_is_served_by_a_signed_url_and_named_by_its_magic(tmp_path: Path, blob: bool) -> None:
    payloads = [b"other", _JPEG]
    payload = blob_field("payload") if blob else pa.field("payload", pa.large_binary())
    values = blob_array(payloads) if blob else pa.array(payloads, pa.large_binary())
    write_table(tmp_path, ["acme", "bronze", "pages"], pa.table({"id": [0, 1], "payload": values}, schema=pa.schema([("id", pa.int64()), payload])))

    with TestClient(_app(tmp_path)) as client:
        url = _sign(client, [{"role": "page", "table": "lake~acme$bronze$pages", "page_id": 1}], dataset=None)[0]["url"]
        served = client.get(str(url))

    assert (served.status_code, served.content, served.headers["content-type"]) == (200, _JPEG, "image/jpeg")


@pytest.mark.parametrize("stored", [pytest.param("text/html", id="html"), pytest.param("image/svg+xml", id="svg")])
def test_a_type_a_browser_would_run_is_served_as_an_inert_attachment(tmp_path: Path, stored: str) -> None:
    schema = pa.schema([pa.field("doc_id", pa.string()), blob_field("media"), pa.field("mime", pa.string())])
    script = b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(document.cookie)'/><script>alert(1)</script>"
    write_table(
        tmp_path, ["wh", "corpus", "docs"], pa.table({"doc_id": ["d1"], "media": blob_array([script]), "mime": [stored]}, schema=schema), descriptor=_DOCUMENTS
    )

    with TestClient(_app(tmp_path)) as client:
        url = str(_sign(client, [{"role": "media", "doc_id": "d1"}])[0]["url"])
        served = client.get(url)
        revalidated = client.get(url, headers={"If-None-Match": served.headers["etag"]})

    assert (served.status_code, served.headers["content-type"], served.headers["content-disposition"]) == (200, "application/octet-stream", "attachment")
    for answer in (served, revalidated):
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_a_plain_payload_too_large_to_serve_whole_is_signed_unread_and_not_served(tmp_path: Path) -> None:
    """Lance reads a plain binary cell only whole, so the signing reads none (it checks the row is there), and the serve,
    which has to read it, serves none past the cap."""
    from viewer.api.v1.endpoints.signed_media import MAX_PLAIN_CELL_BYTES

    payloads = pa.array([_JPEG + b"\x00" * MAX_PLAIN_CELL_BYTES], pa.large_binary())
    write_table(tmp_path, ["acme", "bronze", "pages"], pa.table({"id": [0], "payload": payloads}))

    with TestClient(_app(tmp_path)) as client:
        [signed, absent] = _sign(client, [{"role": "page", "table": "lake~acme$bronze$pages", "page_id": p} for p in (0, 9)], dataset=None)
        served = client.get(str(signed["url"]))

    assert (absent["detail"], served.status_code, served.json()["detail"]) == (
        "no page with id 9 in 'lake~acme$bronze$pages'",
        404,
        f"this payload is plain binary over {MAX_PLAIN_CELL_BYTES} bytes, too large to serve whole",
    )


@pytest.mark.parametrize(
    ("role", "binding"),
    [
        pytest.param("media", {"mime": "image/png"}, id="mime-names-a-type"),
        pytest.param("thumbnail", {"thumbnail": "thumb"}, id="thumbnail-names-no-column"),
        pytest.param("thumbnail", {"thumbnail": "media", "thumbnail_mime": "thumb_mime"}, id="thumbnail-mime-names-no-column"),
    ],
)
def test_a_document_binding_naming_no_column_is_refused_when_the_dataset_opens(tmp_path: Path, role: str, binding: dict[str, str]) -> None:
    """Each of `document.mime`, `thumbnail` and `thumbnail_mime` names a column of the documents table; a descriptor whose
    one names none is refused as the dataset opens, so the batch answers why for the item, not a 500 for the batch."""
    descriptor = {**_DOCUMENTS, "document": {"table": "docs", "media_blob": "media", **binding}}
    write_table(tmp_path, ["wh", "corpus", "docs"], _docs(), descriptor=descriptor)

    with TestClient(_app(tmp_path)) as client:
        answer = _sign(client, [{"role": role, "doc_id": "d1"}])

    [only] = answer
    assert only["url"] is None
    assert "is not a column of 'docs'" in str(only["detail"])


def test_a_key_short_of_32_decoded_bytes_signs_nothing(tmp_path: Path) -> None:
    """A 32-character hex string looks long and carries 16 bytes of entropy; as base64 it decodes to 24 bytes."""
    write_table(tmp_path, ["wh", "corpus", "docs"], _docs(), descriptor=_DOCUMENTS)
    (tmp_path / "media-url-key").write_text("ab" * 16)

    with TestClient(_app(tmp_path)) as client:
        refused = client.post("/api/signed-media", json={"dataset": "lake~wh$corpus", "items": [{"role": "media", "doc_id": "d1"}]})

    assert refused.status_code == 503, refused.text
