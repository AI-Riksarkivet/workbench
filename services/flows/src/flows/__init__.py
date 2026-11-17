"""flows — the studio flow-builder's server half.

Three seams: a server-DECLARED node catalog (so a server-side node kind can appear in the palette without a frontend
release), PURE graph validation shared with the frontend executor's vocabulary, and durable run execution — each run
and each step's outcome are rows in the service's own Postgres (`flows.store`), driven under a lease by any replica
and resumed by any replica's re-drive after a crash (`flows.runner`).

Fleet layout (`make_service_app` + flat modules + an injectable lifespan). The public
namespace is `/api/flows`; the cluster route forwards that rule unrewritten because this app mounts its
routers under `WORKBENCH_API_PREFIX` (`/api` in the chart and in `scripts/local-stack.sh`) itself.
"""

from flows import health, routes
from flows.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="flows",
    routers=[health.router, routes.router],
    lifespan=make_lifespan,
)
