"""Self-repair: the LLM revises SQL that was rejected, failed or returned no rows.

This is the only step where the LLM gets a tool: describe_table, run through
our MCP server, so it can re-check the real columns before revising.
"""

import json
from dataclasses import dataclass, field

import anyio

from db_assistant import config
from db_assistant.pipeline.generate import extract_sql
from db_assistant.pipeline.mcp_client import QueryFailed

EMPTY_RESULT = "The query ran but returned no rows."

INSTRUCTIONS = """You fix SQLite queries for a data analytics assistant.

A query written for the user's question did not work. You get the question,
every attempt so far and what went wrong with each. Write one corrected
read-only SELECT (or WITH ... SELECT) query.

- "Rejected: ..." (validation failure) or "Query failed: ..." (execution
  error): the message names what failed and what is allowed. Fix that.
- No rows returned: first check the types and formats of the values you
  filter on (dates are TEXT YYYY-MM-DD; use the known values for country and
  category). If the filters are right and the data simply has no match,
  broaden the scope so the user still gets a useful answer, for example the
  date range the data covers, or the nearest matching values. Then set
  broadened_scope to true and write note_to_user: one sentence saying what was
  not found and what you show instead.
- Otherwise set broadened_scope to false and leave note_to_user empty.

Before revising, you can call describe_table to check a table's real columns
and types.

Reply with: sql (the query only), reason (what was wrong and what you
changed), broadened_scope, note_to_user.

The database schema is below."""

SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "reason": {"type": "string"},
        "broadened_scope": {"type": "boolean"},
        "note_to_user": {"type": "string"},
    },
    "required": ["sql", "reason", "broadened_scope", "note_to_user"],
    "additionalProperties": False,
}

DESCRIBE_TABLE_TOOL = {
    "name": "describe_table",
    "description": (
        "Describe one table: its columns (name, type, not null, primary key) "
        "and foreign keys. table_name must be one of: "
        + ", ".join(config.ALLOWED_TABLES) + "."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"table_name": {"type": "string"}},
        "required": ["table_name"],
        "additionalProperties": False,
    },
}


@dataclass
class Attempt:
    sql: str
    problem: str | None  # None if the query worked; else the error or EMPTY_RESULT
    tool_calls: list = field(default_factory=list)  # describe_table calls made to write this SQL
    reason: str | None = None  # the LLM's explanation, for repaired SQL


@dataclass
class Repair:
    sql: str
    reason: str
    broadened_scope: bool
    note_to_user: str
    tool_calls: list


def build_user_message(question, attempts):
    parts = [f"Question: {question}"]
    for number, attempt in enumerate(attempts, start=1):
        parts.append(f"Attempt {number}:\nSQL:\n{attempt.sql}\nProblem: {attempt.problem}")
    return "\n\n".join(parts)


async def repair_sql(llm, db, schema_text, question, attempts):
    """Ask the LLM for revised SQL, letting it call describe_table through MCP."""

    # The LLM client is synchronous and the MCP connection is async. So the
    # LLM call runs on a worker thread (to_thread), and each tool call it
    # makes hops back to the event loop to use the connection (from_thread).
    def call_tool(name, arguments):
        if name != DESCRIBE_TABLE_TOOL["name"]:
            return {"content": f"Unknown tool '{name}'. The only tool is describe_table.", "is_error": True}
        try:
            table = anyio.from_thread.run(db.describe_table, arguments.get("table_name", ""))
        except QueryFailed as error:
            return {"content": str(error), "is_error": True}
        return {"content": json.dumps(table), "is_error": False}

    def ask_llm():
        return llm.complete_with_tools(
            system=f"{INSTRUCTIONS}\n\n{schema_text}",
            user_message=build_user_message(question, attempts),
            tools=[DESCRIBE_TABLE_TOOL],
            call_tool=call_tool,
            schema=SCHEMA,
            max_tool_calls=config.MAX_TOOL_CALLS_PER_REPAIR,
        )

    reply, tool_calls = await anyio.to_thread.run_sync(ask_llm)
    return Repair(
        sql=extract_sql(reply["sql"]),
        reason=reply["reason"],
        broadened_scope=reply["broadened_scope"],
        note_to_user=reply["note_to_user"],
        tool_calls=tool_calls,
    )
