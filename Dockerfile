# Aurora Customer Operations Agent — API image.
FROM python:3.12-slim

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (better layer caching).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    LLM_PROVIDER=mock \
    DB_PATH=/data/aurora.db \
    CHROMA_PATH=/data/chroma \
    TRACE_DIR=/data/traces

VOLUME ["/data"]
EXPOSE 8000

# Seed the mock backend on first boot (if empty), then serve.
CMD ["sh", "-c", "test -f $DB_PATH || uv run python -m agent_ops.backend.seed; uv run uvicorn agent_ops.api.main:app --host 0.0.0.0 --port 8000"]
