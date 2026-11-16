"""A catalog that vends each caller its own store credential, and a store that admits only the credentials it was given.

The media plane's zero-trust claims are about what crosses two wires: the bearer and trace context a REST catalog
receives, and the access key an object store receives. pylance's REST and S3 clients are Rust, so both are observed at a
server on a real socket:

- :class:`VendingCatalog` answers ListTables and DescribeTable for one table, vending per bearer the storage options its
  test configured (or refusing the bearer, or failing), and records each request's operation, bearer and ``traceparent``.
- :class:`KeyedStore` is a moto S3 server behind a proxy that reads the access key off each request's signature, records
  it, and refuses every key but the admitted ones with 403, as a store refuses a credential it never issued.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Generator, MutableMapping
from contextlib import contextmanager
from typing import Any

import boto3
import lance
import pyarrow as pa
from moto.server import ThreadedMotoServer


BUCKET = "media-bucket"
#: The store's region; any value a SigV4 signature can name.
REGION = "us-east-1"


class KeyedStore:
    """An S3 endpoint that serves moto's objects to the admitted access keys only, recording every key it is sent."""

    def __init__(self, moto_url: str, admitted: set[str]) -> None:
        self.admitted = admitted
        self.keys: list[str | None] = []
        store = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self) -> None:
                signature = re.search(r"Credential=([^/]+)/", self.headers.get("Authorization") or "")
                key = signature.group(1) if signature else None
                store.keys.append(key)
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else None
                if key not in store.admitted:
                    self._answer(403, b"<Error><Code>InvalidAccessKeyId</Code></Error>", {})
                    return
                forwarded = {k: v for k, v in self.headers.items() if k.lower() in ("range", "content-type")}
                request = urllib.request.Request(moto_url + self.path, data=body, method=self.command, headers=forwarded)
                try:
                    with urllib.request.urlopen(request) as answer:  # noqa: S310 - the test's own moto server
                        self._answer(answer.status, answer.read(), dict(answer.headers))
                except urllib.error.HTTPError as refused:
                    self._answer(refused.code, refused.read(), dict(refused.headers))

            def _answer(self, status: int, data: bytes, headers: dict[str, str]) -> None:
                self.send_response(status)
                for name, value in headers.items():
                    if name.lower() not in ("content-length", "transfer-encoding", "connection", "date", "server"):
                        self.send_header(name, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_HEAD = _serve

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()


def vend(store_url: str, access_key: str, **extra: str) -> dict[str, str]:
    """The storage options a catalog vends for ``access_key`` on ``store_url``, with no retries, so a refusal is final."""
    return {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": f"secret-of-{access_key}",
        "aws_endpoint": store_url,
        "aws_region": REGION,
        "allow_http": "true",
        "client_max_retries": "0",
        **extra,
    }


@contextmanager
def keyed_store(admitted: set[str], table: pa.Table) -> Generator[tuple[KeyedStore, str]]:
    """A :class:`KeyedStore` holding ``table`` at ``s3://media-bucket/<uuid>/chunks.lance``; yields the store and that location."""
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    moto.start()
    host, port = moto.get_host_and_port()
    moto_url = f"http://{host}:{port}"
    s3 = boto3.client("s3", endpoint_url=moto_url, region_name=REGION, aws_access_key_id="seed", aws_secret_access_key="seed")
    s3.create_bucket(Bucket=BUCKET)
    # The proxy forwards unsigned (it has already judged the key), so moto serves the bucket to anyone it is asked by.
    policy = {
        "Effect": "Allow",
        "Principal": "*",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
    }
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({"Version": "2012-10-17", "Statement": [policy]}))
    # moto keeps its buckets process-wide across servers, so each store writes under a prefix of its own.
    location = f"s3://{BUCKET}/{uuid.uuid4().hex}/chunks.lance"
    lance.write_dataset(table, location, storage_options=vend(moto_url, "seed"), data_storage_version="2.2")
    store = KeyedStore(moto_url, admitted)
    try:
        yield store, location
    finally:
        store.close()
        moto.stop()


class VendingCatalog:
    """ListTables and DescribeTable for the one table ``chunks`` in every namespace, answered per bearer.

    ``answers`` maps a bearer to the storage options vended to it, to ``"refuse"`` (the spec's PermissionDenied, 403) or
    to ``"fail"`` (the spec's ServiceUnavailable, 503). Every request lands in ``calls`` as (operation, bearer,
    traceparent).
    """

    def __init__(self, location: str, answers: MutableMapping[str, Any]) -> None:
        self.location = location
        self.answers = answers
        self.calls: list[tuple[str, str | None, str | None]] = []
        catalog = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _record(self, operation: str) -> str | None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                bearer = self.headers.get("Authorization")
                catalog.calls.append((operation, bearer, self.headers.get("traceparent")))
                return bearer

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's handler name
                self._record("list_tables")
                self._json(200, {"tables": ["chunks"]})

            def do_POST(self) -> None:  # noqa: N802 - the stdlib's handler name
                bearer = self._record("describe_table")
                answer = catalog.answers.get((bearer or "").removeprefix("Bearer "), "refuse")
                if answer == "refuse":
                    self._json(403, {"error": "refused", "code": 15})
                elif answer == "fail":
                    self._json(503, {"error": "unavailable", "code": 17})
                else:
                    self._json(200, {"location": catalog.location, "storage_options": answer})

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def described(self) -> int:
        return sum(1 for operation, _bearer, _trace in self.calls if operation == "describe_table")

    def close(self) -> None:
        self._server.shutdown()
