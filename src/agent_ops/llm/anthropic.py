"""Real Claude provider (LLM_PROVIDER=anthropic). Prompts Claude for the agent's
reasoning; shares all prompt/JSON/retry logic with the local provider via
PromptedProvider. Not exercised in the offline default — activated with a key."""

from __future__ import annotations

import time

from agent_ops.config import get_settings
from agent_ops.llm.prompted import PromptedProvider


class AnthropicProvider(PromptedProvider):
    def __init__(self) -> None:
        super().__init__()
        import anthropic  # lazy import

        s = get_settings()
        if not s.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        self._settings = s

    def _complete(
        self, role: str, system: str, user: str, *, want_json: bool, max_tokens: int
    ) -> str:
        model = self._settings.model_for_role(role)
        sys = system + ("\n\nRespond with a single valid JSON object only." if want_json else "")
        t0 = time.perf_counter()
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=sys,
            messages=[{"role": "user", "content": user}],
        )
        latency = (time.perf_counter() - t0) * 1000
        self._record_real(role, model, resp.usage.input_tokens, resp.usage.output_tokens, latency)
        return "".join(b.text for b in resp.content if b.type == "text")
