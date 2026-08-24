"""LLM provider abstraction: mock (offline) and Anthropic (real) behind a router."""

from agent_ops.llm.router import get_provider

__all__ = ["get_provider"]
