"""flows service lifespan — the HTTP client, the run store and this replica's run driver.

**No run store named, no runs:** the store stays unset, the run doors answer 503 naming why
(`flows.dependencies.get_store`), and the catalog, validation and inference doors serve.

**A run store named and unreachable fails the start.** The database is the service's own, so a start that cannot reach
it is a process the platform restarts until it can, rather than one reporting ready while every run door fails.

The driver's re-drive starts here: it resumes, at once and then about every `WORKBENCH_FLOWS_RUN_REDRIVE_SECONDS`,
every run whose driver stopped, in this or any replica (`flows.runner`).
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from flows.config import build_flows_settings
from flows.dependencies import NO_RUN_STORE
from flows.runner import RunRunner
from flows.store import FlowsStore
from workbench.governed.auth_lifespan import attach_auth
from workbench.governed.auth_lifespan import dispose as auth_dispose


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Run inside `make_service_app`'s own lifespan, which has put the shared settings on `app.state.settings`."""
    app.state.flows_settings = build_flows_settings()
    flows_settings = app.state.flows_settings
    # AUTHN/AUTHZ, as every service builds it. Built here, never at import (an OIDCVerifier fetches discovery). A failure to
    # BUILD is logged and non-fatal, so the dependency finds no verifier/client on app.state
    # and answers 503; falling back to a permissive checker would turn a broken authorization layer into an open one.
    await attach_auth(app, flows_settings, service="flows")
    # One app-scoped client so connections are pooled across steps and runs. The per-node budget is passed per call
    # (executor._call_serve); this is the connect/default bound.
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=flows_settings.serve_timeout))
    app.state.run_store = None
    app.state.run_runner = None
    try:
        if flows_settings.state_host is None:
            log.warning("flows: %s; the run doors will 503", NO_RUN_STORE)
        else:
            store = await FlowsStore.open(flows_settings)
            runner = RunRunner(
                store,
                app.state.http,
                lease_seconds=flows_settings.run_lease_seconds,
                redrive_seconds=flows_settings.run_redrive_seconds,
                concurrency=flows_settings.run_concurrency,
                max_attempts=flows_settings.run_max_attempts,
                retention_days=flows_settings.run_retention_days,
            )
            app.state.run_store, app.state.run_runner = store, runner
            runner.start()
        log.info("startup_complete")
        yield
    finally:
        # The drivers stop first, each releasing its lease so another replica resumes its run at once; then the
        # store they write through, then the client their steps call Serve with.
        if app.state.run_runner is not None:
            await app.state.run_runner.close()
        if app.state.run_store is not None:
            await app.state.run_store.close()
        await auth_dispose(app)
        await app.state.http.aclose()
        log.info("shutdown_complete")
