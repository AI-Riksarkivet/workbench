"""The search result cache evicts by BYTES as well as entry count (#141), and is shared safely by the threads the search
routes run on.

256 entries was a count, not a memory bound: a hit row carries display.body (transcript text), so a
full page of hits runs to megabytes and the cache could hold ~30× the pod's headroom while staying
"under" its limit. Eviction now honours a byte ceiling; the entry count remains the lookup bound.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pyarrow as pa

from search.services.result_cache import entry_bytes, run_cached
from search.services.spec import SearchSpec
from tests.media_catalog import lakehouse_dir, write_table
from workbench.governed.token_exchange import LakehouseCaller
from workbench.lancekit.registry import DatasetRegistry
from workbench.media.config import MediaSettings


def _fake_key(monkeypatch: Any, name: str) -> None:
    from search.services import result_cache

    monkeypatch.setattr(result_cache, "cache_key", lambda *_a, **_k: ("ds", "sig", name))


def _run(cache: dict[Any, Any], rows: list[dict[str, Any]], name: str, monkeypatch: Any, *, max_size: int = 10, max_bytes: int = 0) -> list[dict[str, Any]]:
    _fake_key(monkeypatch, name)
    return run_cached(cache, threading.Lock(), max_size, max_bytes, cast(Any, None), cast(Any, None), None, None, lambda: rows)


def test_byte_ceiling_evicts_oldest(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    big = [{"body": "x" * 100}]
    budget = 3 * entry_bytes(big)  # room for three entries, not four
    for name in ("a", "b", "c", "d"):
        _run(cache, big, name, monkeypatch, max_bytes=budget)
    keys = [k[2] for k in cache]
    assert "a" not in keys  # oldest paid for the fourth
    assert keys[-1] == "d"
    assert sum(size for _, size in cache.values()) <= budget


def test_oversized_entry_is_served_but_never_cached(monkeypatch: Any) -> None:
    """One entry bigger than the whole budget must not wipe the cache to store itself."""
    cache: dict[Any, Any] = {}
    _run(cache, [{"body": "small"}], "small", monkeypatch, max_bytes=10_000)
    huge = [{"body": "x" * 20_000}]
    out = _run(cache, huge, "huge", monkeypatch, max_bytes=10_000)
    assert out == huge  # still answered
    assert [k[2] for k in cache] == ["small"]  # cache untouched


def test_zero_byte_bound_keeps_count_only_behaviour(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    for name in ("a", "b", "c"):
        _run(cache, [{"body": "x" * 10_000}], name, monkeypatch, max_size=2, max_bytes=0)
    assert [k[2] for k in cache] == ["b", "c"]  # count bound still enforced


def test_hit_returns_rows_and_refreshes_recency(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    rows = [{"body": "kept"}]
    _run(cache, rows, "a", monkeypatch)
    _run(cache, [{"body": "other"}], "b", monkeypatch)
    hit = _run(cache, [{"body": "NOT recomputed"}], "a", monkeypatch)
    assert hit == rows  # served from cache, produce() ignored
    assert [k[2] for k in cache] == ["b", "a"]  # a moved to newest


_DESCRIPTOR = {"identity": {"key_fields": ["doc_id"]}, "search": {"row_table": "chunks"}}
_ROWS = pa.table({"doc_id": ["a"]}, schema=pa.schema([("doc_id", pa.string())], metadata={b"lance_media.descriptor": json.dumps(_DESCRIPTOR).encode()}))


def test_searches_running_at_once_share_the_cache_and_each_gets_its_answer(tmp_path: Path) -> None:
    """The search routes run in the threadpool, so one app's cache is read, moved and evicted from several threads at
    once. Eight threads ask three questions of a cache that holds two, so every ask evicts another's entry, over a real
    table whose version keys each entry; the interpreter switches threads as often as it can, to reach the gap between
    one thread's read and its write."""
    write_table(tmp_path, ["wh", "ns", "chunks"], _ROWS)
    registry = DatasetRegistry(MediaSettings.model_validate({**lakehouse_dir(str(tmp_path))}))
    handle = registry.get("lake~wh$ns", LakehouseCaller(subject="anon"))
    cache: dict[Any, Any] = {}
    lock = threading.Lock()
    specs = [SearchSpec.model_validate({"q": q}) for q in ("cat", "dog", "owl")]
    failures: list[BaseException] = []
    wrong: list[tuple[str, list[dict[str, Any]]]] = []

    def ask(worker: int) -> None:
        for round_ in range(400):
            spec = specs[(worker + round_) % len(specs)]
            try:
                hits = run_cached(cache, lock, 2, 0, handle, spec, None, None, lambda spec=spec: [{"doc_id": spec.q}])
            except Exception as exc:  # noqa: BLE001 - any exception is the defect this test is for
                failures.append(exc)
                return
            if hits != [{"doc_id": spec.q}]:
                wrong.append((spec.q, hits))

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=ask, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        sys.setswitchinterval(previous)

    assert failures == [], f"a search failed on the shared cache: {failures[0]!r}"
    assert wrong == []
    assert len(cache) <= 2
