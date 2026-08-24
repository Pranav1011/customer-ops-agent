"""Shared fixtures. Seeds a small, isolated mock backend once per test session
in a temp directory so tests never touch the developer's real data."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_backend():
    tmp = Path(tempfile.mkdtemp(prefix="aurora-test-"))
    os.environ["DB_PATH"] = str(tmp / "test.db")
    os.environ["CHROMA_PATH"] = str(tmp / "chroma")
    os.environ["TRACE_DIR"] = str(tmp / "traces")
    os.environ["LLM_PROVIDER"] = "mock"

    # Rebuild cached singletons that captured settings/paths at import time.
    from agent_ops.config import get_settings

    get_settings.cache_clear()

    import agent_ops.backend.db as db
    import agent_ops.backend.kb as kb

    db._engine = None
    kb._client.cache_clear()

    from agent_ops.backend.seed import reset_and_seed

    reset_and_seed()
    yield
