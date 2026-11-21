"""Flows' ``/health``: process liveness only, the badge the chart's ``/api/health`` probe reads.

Liveness, deliberately not a readiness that probes Ray Serve: a probe that fails when a model is unreachable turns "no
GPU today" into a restart loop, and flows with no Serve behind it still serves its catalog, validates a graph and
refuses a model node plainly. The factory's ``/livez`` and ``/readyz`` (``workbench.probes``) are the operational pair.
"""

from fastapi import APIRouter

from workbench.schemas.health import Liveness


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> Liveness:
    # On the event loop, never queued behind the threadpool, which would fail the probe when the pod is busiest.
    return Liveness()
