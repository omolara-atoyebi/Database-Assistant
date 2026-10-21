"""MCP tools, called through a real MCP client connected to the server in-process."""

import sys

import pytest
from mcp import Client, StdioServerParameters

from db_assistant import config
from db_assistant.mcp_server import database
from db_assistant.mcp_server.server import mcp

pytestmark = pytest.mark.anyio

ALLOWED_TABLES_TEXT = "Allowed tables: customers, products, orders, order_items."


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(tool, arguments=None):
    async with Client(mcp) as client:
        return await client.call_tool(tool, arguments or {})


def error_text(result):
    assert result.is_error
    return result.content[0].text


# --- Tool listing -----------------------------------------------------------

async def test_three_read_only_tools_listed():
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert sorted(tool.name for tool in tools) == ["describe_table", "list_tables", "run_query"]
    for tool in tools:
        assert tool.description
        assert tool.annotations.read_only_hint is True


# --- list_tables ------------------------------------------------------------

async def test_list_tables_returns_exactly_the_allowed_tables():
    result = await call("list_tables")
    assert not result.is_error
    assert result.structured_content == {"result": list(config.ALLOWED_TABLES)}


# --- describe_table ---------------------------------------------------------

async def test_describe_table_returns_columns_and_foreign_keys():
    result = await call("describe_table", {"table_name": "orders"})
    assert not result.is_error
    description = result.structured_content
    assert description["table"] == "orders"
    assert [c["name"] for c in description["columns"]] == [
        "id", "customer_id", "order_date", "total_amount",
    ]
    assert description["columns"][0]["primary_key"] is True
    assert description["foreign_keys"] == [
        {"column": "customer_id", "references": "customers.id"}
    ]


@pytest.mark.parametrize(
    "table_name",
    ["sqlite_master", "orders; DROP TABLE orders", "invoices", ""],
)
async def test_describe_table_rejects_unlisted_names_without_running_a_query(
    table_name, monkeypatch
):
    connections_opened = []
    monkeypatch.setattr(database, "connect", lambda: connections_opened.append(1))

    result = await call("describe_table", {"table_name": table_name})

    message = error_text(result)
    assert "Rejected:" in message
    assert ALLOWED_TABLES_TEXT in message
    assert connections_opened == []


# --- run_query --------------------------------------------------------------

async def test_run_query_returns_rows():
    result = await call("run_query", {"sql": "SELECT id, name FROM products WHERE id <= 2"})
    assert not result.is_error
    assert result.structured_content == {
        "columns": ["id", "name"],
        "rows": [[1, "Sci-Fi Novel"], [2, "Board Game"]],
        "row_count": 2,
        "truncated": False,
    }


async def test_run_query_reports_truncation():
    # orders has 300 rows, more than the row limit.
    result = await call("run_query", {"sql": "SELECT id FROM orders"})
    assert result.structured_content["row_count"] == config.ROW_LIMIT
    assert result.structured_content["truncated"] is True


@pytest.mark.parametrize(
    "sql, denied",
    [
        ("DELETE FROM orders", "DELETE"),
        ("WITH x AS (SELECT 1) DELETE FROM orders", "DELETE"),
        ("SELECT name FROM sqlite_master", "sqlite_master"),
        ("SELECT 1; DROP TABLE orders", "one statement"),
    ],
)
async def test_run_query_unsafe_sql_is_a_tool_error_not_a_crash(sql, denied):
    message = error_text(await call("run_query", {"sql": sql}))
    assert "Rejected:" in message
    assert denied in message
    assert ALLOWED_TABLES_TEXT in message


async def test_run_query_runtime_failure_is_a_tool_error():
    message = error_text(await call("run_query", {"sql": "SELECT FROM WHERE"}))
    assert "Query failed:" in message


# --- Real process over stdio ------------------------------------------------

async def test_server_runs_as_a_process_over_stdio():
    # Starts the server the same way MCP Inspector and Claude Code will.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "db_assistant.mcp_server.server"],
        cwd=str(config.PROJECT_ROOT),
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("run_query", {"sql": "SELECT COUNT(*) FROM customers"})
    assert len(tools) == 3
    assert result.structured_content["rows"] == [[30]]
