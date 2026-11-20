"""An `all` search (`GET /api/search?mode=all`) ranks what its legs found by the one reciprocal-rank fusion a search across
several datasets uses (`search.services.fuse`), and says so with each row's fused score.

Driven through the real search app over a real table on the ``dir`` catalog, with a full-text index on its text and a
vector column its descriptor declares; the query's embedding comes from a stand-in encoder, since no model runs here.
Two rows tie: each is first in one leg and second in the other. The tie goes to the one the keyword leg put first, the
better leg-order answer for a person, and not to whichever id sorts first.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
from fastapi.testclient import TestClient

from client_kit.media.lifespan import make_media_state
from search.api.dependencies import get_embedder
from search.core.config import SearchSettings
from search.main import create_search_app
from search.services.fuse import DEFAULT_K
from tests.media_catalog import lakehouse_dir, write_table


_DESCRIPTOR = {
    "identity": {"key_fields": ["doc_id"]},
    "display": {"body": "text"},
    "search": {
        "row_table": "chunks",
        "fts": {"table": "chunks", "column": "text"},
        "vectors": {"semantic": {"table": "chunks", "column": "vec", "dim": 2, "query_encoder": "text"}},
    },
}


class _Encoder:
    """Embeds every query as the same unit vector, so the vector leg ranks by each row's own vector."""

    def embed_text(self, texts: list[str]) -> np.ndarray:
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    def embed_image(self, images: list[bytes]) -> np.ndarray:
        return np.array([[1.0, 0.0] for _ in images], dtype=np.float32)


def test_an_all_search_fuses_its_legs_and_a_tie_goes_to_the_keyword_legs_first(tmp_path: Path) -> None:
    rows = pa.table(
        {
            # The keyword leg ranks `zeta` first (the word twice), then `alpha`; the vector leg ranks `alpha` first (its
            # vector is the query's), then `zeta`; `other` matches no keyword and is last of the vector leg.
            "doc_id": ["zeta", "alpha", "other"],
            "text": ["harbour harbour", "harbour and the long road to the sea", "a cart"],
            "vec": pa.array([[0.6, 0.8], [1.0, 0.0], [-1.0, 0.0]], pa.list_(pa.float32(), 2)),
        }
    )
    ds = write_table(tmp_path, ["wh", "corpus", "chunks"], rows, descriptor=_DESCRIPTOR)
    ds.create_scalar_index("text", index_type="INVERTED")
    settings = SearchSettings.model_validate({**lakehouse_dir(str(tmp_path))})
    app = create_search_app(make_media_state(settings), settings)
    app.dependency_overrides[get_embedder] = lambda: _Encoder

    with TestClient(app) as client:
        response = client.get("/api/search", params={"q": "harbour", "mode": "all", "dataset": "lake~wh$corpus", "n": "3"})

    assert response.status_code == 200, response.text
    hits = pa.ipc.open_stream(response.content).read_all().to_pylist()
    tie = 1.0 / (DEFAULT_K + 1) + 1.0 / (DEFAULT_K + 2)
    assert [(hit["doc_id"], hit["_rrf"]) for hit in hits] == [("zeta", tie), ("alpha", tie), ("other", 1.0 / (DEFAULT_K + 3))]
