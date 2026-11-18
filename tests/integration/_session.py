"""In-memory MCP ClientSession helper for fast protocol-level integration tests."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def mcp_session(workspace: Path, cli: Path, *, tool_groups: str | None = None):
    """Run an in-memory MCP ClientSession bound to ``workspace`` and ``cli``."""
    previous = {k: os.environ.get(k) for k in ("PVSYST_WORKSPACE", "PVSYST_CLI",
                                               "PVSYST_BUILTIN_COMPONENTS", "PVSYST_TOOL_GROUPS")}
    os.environ["PVSYST_WORKSPACE"] = str(workspace)
    os.environ["PVSYST_CLI"] = str(cli)
    os.environ.pop("PVSYST_BUILTIN_COMPONENTS", None)
    os.environ.pop("PVSYST_TOOL_GROUPS", None)

    from pvsyst_auto.api import context
    from pvsyst_auto.api.app import create_mcp_server, mcp

    server = mcp if tool_groups is None else create_mcp_server(tool_groups)
    context._cli = None
    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            c_read, c_write = client_streams
            s_read, s_write = server_streams
            low = server._lowlevel_server
            async with anyio.create_task_group() as tg:
                tg.start_soon(low.run, s_read, s_write, low.create_initialization_options())
                async with ClientSession(c_read, c_write) as session:
                    await session.initialize()
                    yield session
                tg.cancel_scope.cancel()
    finally:
        context._cli = None
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def decode_reply(reply):
    """Return ``(is_error, payload_or_text)`` from a ``CallToolResult``."""
    text = "\n".join(item.text for item in reply.content if hasattr(item, "text"))
    if reply.is_error:
        return True, text
    return False, json.loads(text) if text else reply.structured_content
