"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
