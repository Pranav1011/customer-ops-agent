"""The MCP server exposes the same tools and enforces the same write policy."""

from __future__ import annotations

from agent_ops.mcp_server import build_server


def _text(result) -> str:
    return result.content[0].text


async def test_mcp_lists_all_tools():
    tools = await build_server().list_tools()
    names = {t.name for t in tools}
    assert {"get_order", "issue_refund", "cancel_subscription", "escalate_to_human"} <= names
    # Schema is derived from the Pydantic model (flat, described).
    get_order = next(t for t in tools if t.name == "get_order")
    assert "order_id" in get_order.input_schema["properties"]


async def test_mcp_read_tool_executes():
    r = await build_server().call_tool("get_order", {"order_id": "ORD-000001"})
    assert '"ok": true' in _text(r)


async def test_mcp_write_is_policy_gated():
    # A refund far above the order total / threshold must NOT execute over MCP.
    r = await build_server().call_tool(
        "issue_refund", {"order_id": "ORD-000002", "amount": 999, "reason": "x"}
    )
    txt = _text(r)
    assert "policy" in txt and ("block" in txt or "escalate" in txt)
