"""Runs the pipeline steps in order. The single entry point for the CLI and UI.

  1. Map:      fetch the schema through MCP and build the schema context
  2. Parse:    scope gate (LLM decides), then clarification (LLM decides)
  3. Generate: the LLM writes SQL
  4. Execute:  run it through the MCP server's run_query (guardrails apply)
  5. Result:   rejected, failed or empty -> the LLM repairs it (max 2 repairs)
  6. Format:   the LLM writes the answer; the SQL is cited as it ran
"""

from dataclasses import dataclass, field

import anyio

from db_assistant import config
from db_assistant.pipeline import mcp_client
from db_assistant.pipeline.answer import write_answer
from db_assistant.pipeline.clarify import check_ambiguity, with_clarification
from db_assistant.pipeline.generate import generate_sql
from db_assistant.pipeline.repair import EMPTY_RESULT, Attempt, repair_sql
from db_assistant.pipeline.schema_context import VALUE_LIST_QUERIES, format_schema
from db_assistant.pipeline.scope import IN_SCOPE, check_scope

ANSWERED = "answered"
DECLINED = "declined"  # out of scope or sensitive
NEEDS_CLARIFICATION = "needs_clarification"  # answer holds the follow-up question
FAILED = "failed"  # the query still failed after the last repair


@dataclass
class Result:
    status: str
    answer: str
    reason: str | None = None  # the LLM's reason for a decline or clarification
    sql: str | None = None  # the last SQL that was run
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    error: str | None = None  # the server's message if the last query failed
    attempts: list = field(default_factory=list)  # every Attempt, in order
    scope_note: str | None = None  # set when a repair broadened the question


async def load_schema_text(db):
    tables = [await db.describe_table(name) for name in await db.list_tables()]
    value_lists = {}
    for column, sql in VALUE_LIST_QUERIES.items():
        result = await db.run_query(sql)
        value_lists[column] = [row[0] for row in result["rows"]]
    return format_schema(tables, value_lists)


async def run_sql(db, sql):
    """Return (result, problem). problem is None if the query returned rows."""
    try:
        result = await db.run_query(sql)
    except mcp_client.QueryFailed as error:
        return None, str(error)
    return result, (None if result["row_count"] else EMPTY_RESULT)


async def answer_question_async(question, llm, db, clarification=None, first_sql=None):
    schema_text = await load_schema_text(db)

    if clarification is not None:
        # One clarification round only: the user has answered, so skip the
        # ambiguity check. The scope gate still runs on the combined text.
        question = with_clarification(question, *clarification)

    scope = check_scope(llm, schema_text, question)
    if scope["decision"] != IN_SCOPE:
        return Result(status=DECLINED, answer=scope["message_to_user"], reason=scope["reason"])

    if clarification is None:
        clarity = check_ambiguity(llm, schema_text, question)
        if clarity["ambiguous"]:
            return Result(
                status=NEEDS_CLARIFICATION,
                answer=clarity["clarifying_question"],
                reason=clarity["reason"],
            )

    # first_sql is a debug hook (cli --first-sql) to force a repair.
    sql = first_sql or generate_sql(llm, schema_text, question)
    attempts = []
    tool_calls, repair_reason, scope_note = [], None, None

    while True:
        result, problem = await run_sql(db, sql)
        attempts.append(Attempt(sql=sql, problem=problem, tool_calls=tool_calls, reason=repair_reason))
        if problem is None:
            break
        if len(attempts) > config.MAX_REPAIRS:
            if result is not None:
                break  # it ran but is still empty: answer honestly that nothing matched
            return Result(
                status=FAILED,
                answer=(
                    f"I couldn't answer that: the query still failed after "
                    f"{config.MAX_REPAIRS} repairs."
                ),
                sql=sql,
                error=problem,
                attempts=attempts,
            )

        repair = await repair_sql(llm, db, schema_text, question, attempts)
        sql, tool_calls, repair_reason = repair.sql, repair.tool_calls, repair.reason
        if repair.broadened_scope:
            scope_note = repair.note_to_user

    return Result(
        status=ANSWERED,
        answer=write_answer(llm, question, sql, result, scope_note),
        sql=sql,
        columns=result["columns"],
        rows=result["rows"],
        truncated=result["truncated"],
        attempts=attempts,
        scope_note=scope_note,
    )


def answer_question(question, llm=None, server=None, clarification=None, first_sql=None):
    """Answer one question. Blocking; opens and closes the MCP connection.

    llm: defaults to the real LLMClient. server: an MCPServer object to connect
    in-process (tests); None starts the real server process.
    clarification: (clarifying_question, user_answer) after a
    NEEDS_CLARIFICATION result; pass the original question again with it.
    first_sql: use this instead of the LLM's first SQL (debugging repairs).
    """
    if llm is None:
        from db_assistant.llm.client import LLMClient

        llm = LLMClient()

    async def run():
        async with mcp_client.connect(server) as db:
            return await answer_question_async(question, llm, db, clarification, first_sql)

    return anyio.run(run)
