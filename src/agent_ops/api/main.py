"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_ops.api.routes import router
from agent_ops.backend.db import init_db
from agent_ops.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Aurora Customer Operations Agent",
    description="Submit tickets, inspect run traces, and review escalations.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "llm_provider": s.llm_provider}
