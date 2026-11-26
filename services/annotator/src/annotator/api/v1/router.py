"""Router aggregation for the annotator service (lance-ns ``api/v1/router.py``).

The annotations resource keeps its package layout (its own router composition); assist is a single-module endpoint.
Wire paths stay ``/api/*``.
"""

from fastapi import APIRouter

from annotator.annotations.router import router as annotations_router
from annotator.api.v1.endpoints.assist import router as assist_router
from annotator.api.v1.endpoints.judge import router as judge_router
from annotator.api.v1.endpoints.me import router as me_router
from annotator.api.v1.endpoints.members import router as members_router
from annotator.api.v1.endpoints.project_events import router as project_events_router
from annotator.api.v1.endpoints.projects import router as projects_router
from annotator.api.v1.endpoints.sampling import project_router as sampling_project_router
from annotator.api.v1.endpoints.sampling import router as sampling_router
from annotator.api.v1.endpoints.tasks import router as tasks_router
from annotator.api.v1.endpoints.views import router as views_router


router = APIRouter()
for r in (
    annotations_router,
    assist_router,
    judge_router,
    sampling_router,
    sampling_project_router,
    me_router,
    projects_router,
    project_events_router,
    members_router,
    tasks_router,
    views_router,
):
    router.include_router(r)
