"""Short signed URLs for one Lance blob cell, which a browser's ``<img>`` and ``<video>`` fetch with no token of their own.

A media element sends no ``Authorization`` header, and a DPoP-bound token cannot ride a URL. So the signing door (DPoP,
as every door) checks the person's own catalog read of the table at the version it resolved, and answers a URL naming
that one cell. The link is the permission: the viewer serves it reading exactly that cell as its own identity
(:class:`ServiceReads`), so no person's token is held for it, and any replica serves it. Lance owns most blobs as byte
ranges inside its own files (descriptor kinds 0 to 2, `lance_docs/lance/format/table/blob.md` § Descriptor Kinds), which
no store URL can address, so the viewer serves them all.

The URL's token is ``<payload>.<signature>``: the payload is the :class:`SignedBlob` as JSON (a cell and an expiry, and
nothing about the person), the signature an HMAC-SHA256 over the payload's encoded form with a key read from a mounted
file on every use, never the environment, so a rotated key takes effect on the next request and every URL signed under
the old one is refused. Changing any field of the payload fails the signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Final

import lance
from lance_namespace import LanceNamespace
from pydantic import BaseModel, ConfigDict, ValidationError

from client_kit.lancekit.catalog import bounded, translate_catalog_errors
from client_kit.lancekit.registry import INDEX_CACHE_BYTES, METADATA_CACHE_BYTES, reusable_until
from service_kit.exceptions import ForbiddenError, ServiceUnavailableError


#: The shortest key the viewer signs with: RFC 2104 § 3 sets the floor at L, the hash's output length, 32 bytes for
#: SHA-256 (its block is 64).
MIN_KEY_BYTES: Final = 32
#: How many (table, version) opens the viewer keeps for signed reads, least recently used dropped first.
SERVICE_OPENS: Final = 64


class SignedBlob(BaseModel):
    """What one signed URL serves: one cell (a catalog table id, a version, a stable row id, a column) until ``exp``.

    ``mime`` is what the signing door read beside the cell; ``None`` means the GET names the bytes by their magic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: list[str]
    version: int
    row: int
    column: str
    mime: str | None
    exp: int

    @property
    def etag(self) -> str:
        """The strong ETag of these bytes: a table version is immutable, so (table, version, row, column) names them."""
        digest = hashlib.sha256("\x00".join((*self.table, str(self.version), str(self.row), self.column)).encode()).hexdigest()
        return f'"{digest[:32]}"'


def now() -> float:
    """The clock a URL's expiry is measured on."""
    return time.time()


def read_key(path: Path | None) -> bytes:
    """The signing key in the mounted file at ``path``, read now.

    The file holds the key as base64 (RFC 4648 § 4, surrounding whitespace ignored), as ``openssl rand -base64 32``
    writes it, and the key is the decoded bytes: at least :data:`MIN_KEY_BYTES` of them, counted after decoding, so text
    that merely looks long (a hex string carries half its length in bytes) is refused.

    Raises:
        ServiceUnavailableError: no key file is configured, or it cannot be read, is not base64, or decodes to too short a key.
    """
    if path is None:
        raise ServiceUnavailableError("this viewer names no media URL key: set VIEWER_MEDIA_URL_KEY_FILE")
    try:
        text = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ServiceUnavailableError(f"the media URL key at {path} could not be read") from exc
    try:
        key = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ServiceUnavailableError(f"the media URL key at {path} is not base64") from exc
    if len(key) < MIN_KEY_BYTES:
        raise ServiceUnavailableError(f"the media URL key at {path} decodes to fewer than {MIN_KEY_BYTES} bytes")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, payload: str) -> bytes:
    return hmac.new(key, payload.encode(), hashlib.sha256).digest()


def sign(blob: SignedBlob, key: bytes) -> str:
    """The URL token naming ``blob``."""
    payload = _b64(blob.model_dump_json().encode())
    return f"{payload}.{_b64(_mac(key, payload))}"


def verify(token: str, key: bytes) -> SignedBlob:
    """The cell ``token`` names, when ``key`` signed it and it has not expired.

    Raises:
        ForbiddenError: the token is malformed, its signature is not ``key``'s over its payload, or it has expired.
    """
    payload, _, signature = token.partition(".")
    try:
        expected = _mac(key, payload)
        presented = _unb64(signature)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ForbiddenError("the media URL is malformed") from exc
    if not payload or not hmac.compare_digest(expected, presented):
        raise ForbiddenError("the media URL's signature does not match")
    try:
        blob = SignedBlob.model_validate_json(_unb64(payload))
    except (ValueError, ValidationError) as exc:
        raise ForbiddenError("the media URL is malformed") from exc
    if blob.exp <= now():
        raise ForbiddenError("the media URL has expired")
    return blob


class _Open(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ds: lance.LanceDataset
    until: float


class ServiceReads:
    """The viewer's own opens of signed tables, one per (table, version), each reused until shortly before its vend expires.

    One session, since every open here is the one identity's (`client_kit.lancekit.registry.CallerSessions` says why a
    session is never shared between identities). A signed GET then costs no DescribeTable while its table's open lasts,
    however many cells of the table a page shows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._opens: OrderedDict[tuple[tuple[str, ...], int], _Open] = OrderedDict()
        self._session = lance.Session(index_cache_size_bytes=INDEX_CACHE_BYTES, metadata_cache_size_bytes=METADATA_CACHE_BYTES)

    def dataset(self, client: Callable[[], LanceNamespace], table: list[str], version: int) -> lance.LanceDataset:
        """``table`` at ``version``, opened through ``client()`` on the storage options the catalog vends the viewer."""
        key = (tuple(table), version)
        with self._lock:
            cached = self._opens.get(key)
            if cached is not None and cached.until > time.monotonic():
                self._opens.move_to_end(key)
                return cached.ds
        with translate_catalog_errors():
            ds = bounded(lambda: lance.dataset(namespace_client=client(), table_id=table, version=version, session=self._session))
        with self._lock:
            self._opens[key] = _Open(ds=ds, until=reusable_until(ds.initial_storage_options))
            self._opens.move_to_end(key)
            while len(self._opens) > SERVICE_OPENS:
                self._opens.popitem(last=False)
        return ds
