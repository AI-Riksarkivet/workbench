"""Version history + time-travel — the compare-versions read-side.

The audit trail of the write plane: who = ``reviewer``, when = the Lance version's
timestamp, what = lineage; this module serves the WHEN (per-unit history + historical
snapshots) that powers the annotator's compare-versions panel.

The history is the annotations table's own Lance version lineage (``ds.versions()``), read off the table opened
through the namespace as the caller: the versions a save commits through the catalog are that table's versions.

Each row costs a read (a manifest open plus a filtered count). They are issued through a bounded, process-wide pool
rather than in a ``for`` loop, and counted by pushdown rather than by materializing the matching ids — see
:data:`VERSION_FANOUT`.
"""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Annotated, Final

import lance
from fastapi import APIRouter, Query
from pydantic import BaseModel

from annotator.annotations.access import MAY_READ_UNIT, UnitKey
from annotator.annotations.unit import parse_unit
from annotator.api.security import AnnotationsDep, CallerDep
from annotator.corpus import resolve
from client_kit.media.deps import DatasetParam, StateDep
from service_kit.exceptions import NotFoundError


router = APIRouter(tags=["annotate"])

#: How many versions this module reads at once, PROCESS-wide.
#:
#: A history answers one row per version, and every row costs a manifest open plus a filtered count. Walking them in a `for` loop made the response
#: `limit` × that latency — up to 200 of them in series, against S3, while holding a threadpool
#: worker. The fan-out below is what removes the series; the bound is what stops the cure being
#: worse, and it is PROCESS-wide rather than per request precisely so N concurrent histories cannot
#: multiply into N × fan-out threads.
VERSION_FANOUT: Final = 8

#: Lazily-threaded (a `ThreadPoolExecutor` starts no worker until something is submitted), so
#: importing this module — which every annotator process does — costs nothing.
_VERSION_POOL: Final = ThreadPoolExecutor(max_workers=VERSION_FANOUT, thread_name_prefix="annotation-versions")


def _in_order[T, R](items: list[T], work: Callable[[T], R]) -> list[tuple[T, R]]:
    """Run `work` over `items` with the bounded pool, dropping the ones that are GONE.

    Results come back in INPUT order, which the newest-first contract depends on, and a
    `NotFoundError` skips its own entry rather than failing the whole listing — the retention race
    the sequential loop already handled, kept handled.
    """
    pending: list[tuple[T, Future[R]]] = [(item, _VERSION_POOL.submit(work, item)) for item in items]
    done: list[tuple[T, R]] = []
    for item, future in pending:
        try:
            done.append((item, future.result()))
        except NotFoundError:
            continue
    return done


class AnnotationVersion(BaseModel):
    """One point in a unit's edit history — a Lance version + when it was committed +
    how many annotations this unit had at it (the audit/compare-versions trail)."""

    version: int
    timestamp: str
    count: int


@router.get("/annotations/{key:path}/versions", dependencies=[MAY_READ_UNIT])
def annotation_versions(
    caller: CallerDep,
    state: StateDep,
    annotations: AnnotationsDep,
    key: UnitKey,
    dataset: DatasetParam = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[AnnotationVersion]:
    """The unit's edit history (most-recent first, capped): each Lance version + its
    timestamp + the count of THIS unit's annotations at it. Powers the compare-versions
    panel — the read-side of the write-plane provenance story (who=reviewer, when=version,
    what=lineage)."""
    corpus = resolve(state, dataset, caller)
    unit = parse_unit(corpus.handle.descriptor, key)
    ds = annotations(corpus.id).open()
    if ds is None:
        return []
    return table_annotation_versions(ds, unit.where, limit=limit)


def table_annotation_versions(ds: lance.LanceDataset, where: str, *, limit: int) -> list[AnnotationVersion]:
    """The unit's history off the table's own version lineage, newest first and capped.

    Counted by PUSHDOWN — `count_rows(filter=...)` — rather than by materializing the matching ids, and read through the
    bounded pool (:data:`VERSION_FANOUT`), because the manifest opens are independent of each other and the latency is
    the whole cost.
    """
    listed = list(reversed(ds.versions()))[:limit]
    counted = _in_order(listed, lambda v: int(checkout(ds, int(v["version"])).count_rows(filter=where)))
    return [AnnotationVersion(version=int(v["version"]), timestamp=iso_timestamp(v.get("timestamp")), count=count) for v, count in counted]


def iso_timestamp(ts: object) -> str:
    """A Lance version timestamp → ISO string (datetime or already-string)."""
    isofmt = getattr(ts, "isoformat", None)
    return isofmt() if callable(isofmt) else str(ts or "")


def checkout(ds: lance.LanceDataset, version: int) -> lance.LanceDataset:
    """Time-travel to a version, translating Lance's raw not-found (an out-of-range or
    reclaimed version) into a clean NotFoundError — mirroring ``table_dataset`` so a bad
    ``?version`` is a 404, not an opaque 500. Only the NOT-FOUND family maps to 404;
    operational failures (permissions, connectivity — this table can live on S3)
    propagate honestly instead of masquerading as a missing version."""
    try:
        return ds.checkout_version(version)
    except (ValueError, FileNotFoundError) as e:
        raise NotFoundError(f"annotations version {version} not found") from e
    except OSError as e:
        if "not found" in str(e).lower():
            raise NotFoundError(f"annotations version {version} not found") from e
        raise
