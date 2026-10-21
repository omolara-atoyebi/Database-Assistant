"""MCP server exposing analytics.db through three read-only tools.

Only run_query accepts SQL, and it goes through the guardrails in database.py.
list_tables and describe_table are built from config.ALLOWED_TABLES and never
run SQL supplied by a caller.

Run it (stdio transport):  python -m db_assistant.mcp_server.server
"""

from typing import Any, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from db_assistant import config
from db_assistant.mcp_server import database
from db_assistant.mcp_server.guardrails import QueryRejected, allowed_tables_text

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


# Return shapes. TypedDict gives clients a structured result and output schema.
class Column(TypedDict):
    name: str
    type: str
    not_null: bool
    primary_key: bool


class ForeignKey(TypedDict):
    column: str
    references: str


class TableDescription(TypedDict):
    table: str
    columns: list[Column]
    foreign_keys: list[ForeignKey]


class QueryRows(TypedDict):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool

mcp = MCPServer(
    name="analytics-db",
    instructions=(
        "Read-only access to a small SQLite e-commerce analytics database. "
        "Call list_tables and describe_table to learn the schema, then run_query "
        "with a single SQLite SELECT statement. "
        + allowed_tables_text()
    ),
)


@mcp.tool(annotations=READ_ONLY)
def list_tables() -> list[str]:
    """List the tables you are allowed to query."""
    return list(config.ALLOWED_TABLES)


@mcp.tool(annotations=READ_ONLY)
def describe_table(table_name: str) -> TableDescription:
    """Describe one table: its columns (name, type, not null, primary key) and
    foreign keys. table_name must be one of the tables from list_tables."""
    if table_name not in config.ALLOWED_TABLES:
        raise ToolError(
            f"Rejected: table '{table_name}' is not allowed. " + allowed_tables_text()
        )

    # Fixed SQL; the already-checked table name is passed as a bound parameter.
    conn = database.connect()
    try:
        columns = conn.execute(
            'SELECT name, type, "notnull", pk FROM pragma_table_info(?) ORDER BY cid',
            (table_name,),
        ).fetchall()
        foreign_keys = conn.execute(
            'SELECT "from", "table", "to" FROM pragma_foreign_key_list(?)',
            (table_name,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "table": table_name,
        "columns": [
            {
                "name": name,
                "type": col_type,
                "not_null": bool(not_null),
                "primary_key": bool(pk),
            }
            for name, col_type, not_null, pk in columns
        ],
        "foreign_keys": [
            {"column": column, "references": f"{ref_table}.{ref_column}"}
            for column, ref_table, ref_column in foreign_keys
        ],
    }


RUN_QUERY_DESCRIPTION = (
    "Run one read-only SQLite SELECT (or WITH ... SELECT) query and return the "
    f"rows. Results are capped at {config.ROW_LIMIT} rows (\"truncated\" is true "
    f"if there were more) and queries time out after "
    f"{config.QUERY_TIMEOUT_SECONDS} seconds. Dates are TEXT in YYYY-MM-DD "
    "format. " + allowed_tables_text()
)


@mcp.tool(annotations=READ_ONLY, description=RUN_QUERY_DESCRIPTION)
def run_query(sql: str) -> QueryRows:
    try:
        result = database.run_query(sql)
    except QueryRejected as error:
        raise ToolError(f"Rejected: {error}") from error
    except database.QueryError as error:
        raise ToolError(f"Query failed: {error}") from error

    return {
        "columns": result.columns,
        "rows": [list(row) for row in result.rows],
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }


if __name__ == "__main__":
    mcp.run()
