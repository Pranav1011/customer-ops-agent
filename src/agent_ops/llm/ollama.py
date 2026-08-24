"""Local LLM provider via Ollama (LLM_PROVIDER=ollama).

A genuinely free, offline LLM brain: the agent's intent classification, planning,
tool decisions, and replies are produced by a local model (default llama3.1:8b)
served by Ollama. Same agent loop, tools, guardrails, memory, tracing, and evals
as every other provider — only the reasoning source changes. Tokens and latency
are recorded; cost is $0 (local).
"""

from __future__ import annotations

import time

import httpx

from agent_ops.config import get_settings
from agent_ops.llm.prompted import PromptedProvider


class OllamaProvider(PromptedProvider):
    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self._base = s.ollama_base_url.rstrip("/")
        self._model = s.ollama_model
        # Local 8B models are slow; give each call generous headroom.
        self._client = httpx.Client(timeout=180.0)

    def _complete(
        self, role: str, system: str, user: str, *, want_json: bool, max_tokens: int
    ) -> str:
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0.2 if want_json else 0.4, "num_predict": max_tokens},
        }
        if want_json:
            payload["format"] = "json"

        t0 = time.perf_counter()
        resp = self._client.post(f"{self._base}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        latency = (time.perf_counter() - t0) * 1000

        self._record_real(
            role,
            self._model,
            int(data.get("prompt_eval_count", 0) or 0),
            int(data.get("eval_count", 0) or 0),
            latency,
        )
        return (data.get("message") or {}).get("content", "")
