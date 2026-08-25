"""Expose the Aurora tool layer as an MCP server (stdio).

The same registry the agent uses is surfaced over the Model Context Protocol, so
the tools can be driven from Claude Desktop, Cursor, or any MCP client. Each
tool's input schema is generated from its Pydantic args model (fields, types, and
descriptions), and write tools are gated through the very same policy engine the
agent obeys — so a client can't use MCP to bypass a guardrail (a >$100 refund
still requires human approval; unverified identity still blocks address changes).

Run: `make mcp` (or `python -m agent_ops.mcp_server`).
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

import agent_ops.tools  # noqa: F401  (registers read/write tools)
from agent_ops.policy.engine import evaluate_action
from agent_ops.tools.registry import REGISTRY, ToolContext, ToolSpec

_INSTRUCTIONS = (
    "Aurora customer-operations tools for a simulated e-commerce/SaaS backend. "
    "Read tools are ungated; write tools (refunds, cancellations, address/CRM "
    "changes, credits) are enforced by Aurora's policy engine and may be blocked "
    "or require human approval."
)


def _make_wrapper(spec: ToolSpec):
    """Build a function whose signature mirrors the tool's Pydantic args model,
    so the MCP client sees a flat, typed, described input schema."""
    params: list[inspect.Parameter] = []
    for fname, finfo in spec.args_model.model_fields.items():
        annotation = (
            Annotated[finfo.annotation, Field(description=finfo.description)]
            if finfo.description
            else finfo.annotation
        )
        default = inspect.Parameter.empty if finfo.is_required() else finfo.default
        params.append(
            inspect.Parameter(
                fname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
            )
        )

    gated = spec.kind == "write" and spec.name != "escalate_to_human"

    def wrapper(**kwargs: Any) -> dict[str, Any]:
        if gated:
            decision = evaluate_action(
                spec.name,
                kwargs,
                authorized_customer=kwargs.get("customer_id"),
                identity_verified=kwargs.get("identity_verified"),
            )
            if not decision.allowed:
                return {
                    "ok": False,
                    "policy": decision.effect,
                    "rule": decision.rule,
                    "reason": decision.reason,
                }
        return REGISTRY.run(spec.name, kwargs, ToolContext(run_id="mcp")).to_dict()

    wrapper.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    wrapper.__name__ = spec.name
    wrapper.__doc__ = spec.description
    return wrapper


def build_server() -> MCPServer:
    server = MCPServer("aurora-ops", instructions=_INSTRUCTIONS)
    for spec in REGISTRY.specs():
        server.add_tool(
            _make_wrapper(spec), name=spec.name, description=f"[{spec.kind}] {spec.description}"
        )
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
