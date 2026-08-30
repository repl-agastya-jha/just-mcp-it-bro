"""Smoke test for the Jira MCP server.

Connects to the running server over streamable HTTP (proving transport and
bearer auth work) and calls get_issue('IMR-1').

- With no Jira credentials configured, the server must return the graceful
  'not configured' error dict — that is the expected PASS in a fresh checkout.
- With credentials configured, any non-crash structured response passes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from config import load_settings
from server import NOT_CONFIGURED_ERROR


def _unwrap(result: object) -> object:
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return result


async def run() -> int:
    settings = load_settings()
    headers = (
        {"Authorization": f"Bearer {settings.mcp_token}"} if settings.mcp_token else {}
    )
    transport = StreamableHttpTransport(
        f"http://127.0.0.1:{settings.port}/mcp", headers=headers
    )

    async with Client(transport) as client:
        result = await client.call_tool("get_issue", {"key": "IMR-1"})
        data = _unwrap(result)

    if not settings.credentials_present:
        if isinstance(data, dict) and data.get("error") == NOT_CONFIGURED_ERROR["error"]:
            print("PASS get_issue: graceful not-configured error returned")
            print("     transport + auth verified without Jira credentials")
            return 0
        print(f"FAIL get_issue: expected {NOT_CONFIGURED_ERROR!r}, got {data!r}")
        return 1

    if isinstance(data, dict):
        summary = " ".join(str(data).split())[:140]
        print(f"PASS get_issue: {summary}")
        return 0
    print(f"FAIL get_issue: unexpected response {data!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
