# app/tools/mcp_client.py

import os
import asyncio
import json
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

# MCP server endpoint (note the /mcp path)
MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://35.175.200.116:8001/mcp")


async def _call_tool_async(tool_name: str, args: dict):
    """
    Async helper that:
    - creates a Client with HTTP transport
    - connects using `async with`
    - calls the tool
    - unwraps CallToolResult into a plain Python value
    """
    transport = StreamableHttpTransport(url=MCP_BASE_URL)
    async with Client(transport) as client:
        result = await client.call_tool(tool_name, args)

        # FastMCP returns a CallToolResult with `.content` list
        # Our tools return dicts, which are encoded as JSON blocks.
        content = getattr(result, "content", None)
        if not content:
            return None

        first = content[0]

        # Case 1: JSON-like block (preferred)
        if hasattr(first, "data") and first.data is not None:
            return first.data

        # Case 2: text block that may contain JSON
        if hasattr(first, "text"):
            text = first.text
            try:
                return json.loads(text)
            except Exception:
                return text  # plain string fallback

        # Fallback: just return the raw result as dict if possible
        try:
            return result.model_dump()
        except Exception:
            return result


def call_tool(tool_name: str, args: dict | None = None):
    """
    Synchronous wrapper used by the rest of your app.

    Example:
        data = call_tool("list_messages", {"q": "is:unread"})
        # `data` is now a dict like {"messages": [...]} from your server tool.
    """
    args = args or {}
    return asyncio.run(_call_tool_async(tool_name, args))
