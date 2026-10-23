"""Connects the pipeline to our MCP server.

The pipeline never opens analytics.db itself: every schema lookup and query
goes through the server's tools, so the server's guardrails always apply.

By default the server is started as a subprocess over stdio, the same way
Claude Code starts it (.mcp.json). Tests pass the server object instead to
connect in-process.
"""

import sys
from contextlib import AsyncExitStack, asynccontextmanager

from mcp import Client, StdioServerParameters

from db_assistant import config


class QueryFailed(Exception):
    """The server rejected a tool call or the query failed. The message is the server's."""


class ServerUnavailable(Exception):
    """The MCP server could not be started, or the connection to it broke."""


def server_params():
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "db_assistant.mcp_server.server"],
        cwd=str(config.PROJECT_ROOT),
    )


class DatabaseTools:
    """The three MCP tools as plain async methods that return Python values."""

    def __init__(self, client):
        self.client = client

    async def _call(self, tool, arguments=None):
        try:
            result = await self.client.call_tool(tool, arguments or {})
        except Exception as error:
            # Tool errors come back as results (below); an exception here
            # means the transport itself failed.
            raise ServerUnavailable(f"Lost the connection to the MCP server: {error!r}") from error
        if result.is_error:
            raise QueryFailed(result.content[0].text)
        return result.structured_content

    async def list_tables(self):
        return (await self._call("list_tables"))["result"]

    async def describe_table(self, table_name):
        return await self._call("describe_table", {"table_name": table_name})

    async def run_query(self, sql):
        """Return {"columns", "rows", "row_count", "truncated"}; raise QueryFailed on error."""
        return await self._call("run_query", {"sql": sql})


@asynccontextmanager
async def connect(server=None):
    """Open a connection: `async with connect() as db: await db.run_query(...)`.

    server: an MCPServer object for in-process use (tests); None starts the
    real server process.
    """
    async with AsyncExitStack() as stack:
        # Only failures while connecting become ServerUnavailable; errors
        # raised inside the `async with` body pass through unchanged.
        try:
            client = await stack.enter_async_context(
                Client(server if server is not None else server_params())
            )
        except Exception as error:
            raise ServerUnavailable(f"Could not start or connect to the MCP server: {error!r}") from error
        yield DatabaseTools(client)
