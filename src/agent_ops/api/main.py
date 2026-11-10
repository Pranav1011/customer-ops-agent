"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_ops.api.routes import router
from agent_ops.backend.db import init_db
from agent_ops.config import get_settings
from agent_ops.reliability.reconcile import reconcile_orphans

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Crash recovery: re-dispatch jobs a dead worker or process left unfinished.
    # A queue outage must not keep the API down, so a failure here is logged.
    try:
        requeued = reconcile_orphans()
        if requeued:
            log.warning(
                "startup reconcile re-queued %d orphaned job(s): %s", len(requeued), requeued
            )
    except Exception:
        log.exception("startup reconcile failed; unfinished jobs were left as they are")
    yield


app = FastAPI(
    title="Aurora Customer Operations Agent",
    description="Submit tickets, inspect run traces, and review escalations.",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow the local Vite dev server (frontend console) to call the API — any
# localhost port, since Vite may pick 5173/5174/… depending on what's free.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "llm_provider": s.llm_provider}
