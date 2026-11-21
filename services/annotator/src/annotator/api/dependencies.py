"""DI aliases for the annotator service — the lance-ns ``api/dependencies.py`` convention.

Annotated aliases over ``app.state`` come from the shared kernel; the state store and the publish runner are the
annotator's own, built by its lifespan (`annotator.main`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from annotator.projects.lakehouse import PublishRunner
from annotator.state.store import AnnotatorStore
from workbench.exceptions import ServiceUnavailableError
from workbench.media.deps import DatasetParam, StateDep


__all__ = ["DatasetParam", "PublishRunnerDep", "StateDep", "StoreDep"]

#: Why the project and task plane answers 503 when the lifespan opened no state store.
NO_STATE_STORE = "the annotator names no state database (ANNOTATOR_STATE_HOST is unset)"


def get_store(request: Request) -> AnnotatorStore:
    """The annotator's state store, or 503 naming why it has none: the read plane serves without one."""
    store: AnnotatorStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise ServiceUnavailableError(f"the project and task plane is unavailable: {NO_STATE_STORE}")
    return store


def get_publish_runner(request: Request) -> PublishRunner:
    runner: PublishRunner | None = getattr(request.app.state, "publish_runner", None)
    if runner is None:
        raise ServiceUnavailableError(f"the publish runner is unavailable: {NO_STATE_STORE}")
    return runner


StoreDep = Annotated[AnnotatorStore, Depends(get_store)]
PublishRunnerDep = Annotated[PublishRunner, Depends(get_publish_runner)]
