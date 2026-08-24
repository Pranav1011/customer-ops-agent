"""Provider router: pick the LLM provider from configuration.

A fresh provider instance is returned per run so usage accounting never leaks
between concurrent tickets.
"""

from __future__ import annotations

from agent_ops.config import get_settings
from agent_ops.llm.base import LLMProvider


def get_provider() -> LLMProvider:
    provider = get_settings().llm_provider.lower()
    if provider == "anthropic":
        from agent_ops.llm.anthropic import AnthropicProvider

        return AnthropicProvider()
    from agent_ops.llm.mock import MockProvider

    return MockProvider()
