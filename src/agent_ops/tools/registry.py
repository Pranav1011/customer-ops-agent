"""Tool registry: the single place the agent, the MCP server, and the eval
harness all discover and invoke tools through.

Every tool has a typed Pydantic args model (validated before execution and
surfaced to the LLM as a JSON schema) and a docstring the model reads. Write
tools additionally declare that they mutate state, so the policy layer knows to
gate them (Phase 2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass
class ToolContext:
    """Per-run execution context passed to every tool.

    `customer_id` is the *authorized* customer for this ticket — write tools use
    it to enforce the "no action outside the current customer" guardrail.
    """

    run_id: str = ""
    ticket_id: str | None = None
    customer_id: str | None = None


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


@dataclass
class ToolSpec:
    name: str
    description: str
    kind: str  # "read" | "write"
    args_model: type[BaseModel]
    func: Callable[[ToolContext, BaseModel], ToolResult]
    reversible: bool = True

    def json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "parameters": self.args_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, kind: str | None = None) -> list[ToolSpec]:
        return [s for s in self._tools.values() if kind is None or s.kind == kind]

    def schemas_for_llm(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [s.json_schema() for s in self.specs(kind)]

    def run(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate args against the tool schema and execute. Never raises for
        bad args or tool-level failures — returns a ToolResult the agent must
        inspect (mirrors how a real tool call surfaces errors)."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(ok=False, error=f"unknown_tool: {name}")
        try:
            validated = spec.args_model(**(args or {}))
        except ValidationError as e:
            return ToolResult(ok=False, error=_format_validation_error(e))
        return spec.func(ctx, validated)


def _format_validation_error(e: ValidationError) -> str:
    """Turn a Pydantic error into a short, LLM-friendly message the model can
    act on (instead of a raw error object with a docs URL)."""
    parts = []
    for err in e.errors():
        field = ".".join(str(x) for x in err.get("loc", ())) or "args"
        got = err.get("input")
        parts.append(f"{field}: {err.get('msg', 'invalid')} (got {got!r})")
    return "invalid_args: " + "; ".join(parts)


REGISTRY = ToolRegistry()


def register(
    *,
    name: str,
    description: str,
    kind: str,
    args_model: type[BaseModel],
    reversible: bool = True,
) -> Callable[
    [Callable[[ToolContext, BaseModel], ToolResult]], Callable[[ToolContext, BaseModel], ToolResult]
]:
    """Decorator to register a tool function."""

    def deco(fn: Callable[[ToolContext, BaseModel], ToolResult]):
        REGISTRY.register(
            ToolSpec(
                name=name,
                description=description,
                kind=kind,
                args_model=args_model,
                func=fn,
                reversible=reversible,
            )
        )
        return fn

    return deco
