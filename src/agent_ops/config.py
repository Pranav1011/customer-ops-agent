"""Central configuration. Read from the environment / an optional .env file.

Everything has a sensible default so the system runs offline with no setup.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = three levels up from this file (src/agent_ops/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: str = Field(default="mock")  # "mock" | "anthropic"
    anthropic_api_key: str = Field(default="")
    model_classifier: str = Field(default="claude-haiku-4-5-20251001")
    model_reasoner: str = Field(default="claude-sonnet-5")
    model_judge: str = Field(default="claude-sonnet-5")

    # --- Storage (relative paths are resolved against the repo root) ---
    db_path: str = Field(default="data/aurora.db")
    chroma_path: str = Field(default="data/chroma")
    trace_dir: str = Field(default="data/traces")

    # --- Agent safety limits ---
    max_iterations: int = Field(default=8)
    cost_ceiling_usd: float = Field(default=0.50)
    refund_approval_threshold: float = Field(default=100.0)
    credit_direct_limit: float = Field(default=25.0)  # goodwill credit auto-apply ceiling
    confidence_threshold: float = Field(default=0.6)  # below this, a write escalates

    def _resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def db_file(self) -> Path:
        return self._resolve(self.db_path)

    @property
    def chroma_dir(self) -> Path:
        return self._resolve(self.chroma_path)

    @property
    def traces_dir(self) -> Path:
        return self._resolve(self.trace_dir)

    @property
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.db_file}"

    def model_for_role(self, role: str) -> str:
        return {
            "classifier": self.model_classifier,
            "reasoner": self.model_reasoner,
            "judge": self.model_judge,
        }.get(role, self.model_reasoner)


@lru_cache
def get_settings() -> Settings:
    return Settings()
