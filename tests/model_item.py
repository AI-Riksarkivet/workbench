"""The item the annotator's model tests ask about, and what the running stand-in endpoint was sent: shared by the assist
and judge tests through the fixtures of `services/annotator/tests/conftest.py` (`endpoint`, `asks`, `client_for`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from fastapi.testclient import TestClient
from pydantic import BaseModel


SOURCE = "scans"
DATASET = f"{SOURCE}~acme$proj1"
DOC = "a" * 16
UNIT = f"{DOC}/0/19"
QUERY = {"project": "p1", "dataset": DATASET}
WORK = {"project": "p1", "task": "t1", "dataset": DATASET}
#: The unit's frame: what a model is sent, byte for byte.
PICTURE = b"\x89PNG\r\n\x1a\n-a-picture"
#: The model a dataset built with `vectors` says made its embeddings.
EMBEDDER = "stand-in-embedder"


class StandIn(BaseModel):
    """The running stand-in endpoint: its address, and every call it was sent since a test last took it."""

    url: str
    asks: list[dict[str, object]]


class Build(Protocol):
    """Builds the client of an annotator whose project p1 holds task t1 (`client_for`)."""

    def __call__(
        self,
        endpoint: str | None,
        *,
        timeout: float = 30.0,
        tools: Sequence[str] = ("bbox", "polygon"),
        presents: str = "the-annotators-own-token",
        picture: bytes = PICTURE,
        judge: str = "",
        tools_of: dict[str, Sequence[str]] | None = None,
        more_docs: Sequence[str] = (),
        vectors: Mapping[str, Sequence[float]] | None = None,
        confidences: Mapping[str, float] | None = None,
        scored_by: str = "",
        searchable: bool = False,
        default_dataset: bool = False,
    ) -> TestClient: ...
