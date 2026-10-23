"""Runs the pipeline steps in order. The single entry point for the CLI and UI.

  1. Map:      fetch the schema through MCP and build the schema context
  2. Parse:    scope gate (LLM decides), then clarification (LLM decides)
  3. Generate: the LLM writes SQL
  4. Execute:  run it through the MCP server's run_query (guardrails apply)
  5. Result:   rejected, failed or empty -> the LLM repairs it (max 2 repairs)
  6. Format:   the LLM writes the answer; the SQL is cited as it ran

Everything a caller needs to display is on the Result, so the UI holds no
pipeline logic. Failures (no API key, MCP server down, LLM errors) come back
as a Result with status ERROR and a friendly message, not as exceptions.
"""

from dataclasses import dataclass, field

import anyio

from db_assistant import config
from db_assistant.llm.client import LLMError, LLMNotConfigured, LLMRefusal
from db_assistant.pipeline import mcp_client
from db_assistant.pipeline.answer import write_answer
from db_assistant.pipeline.clarify import check_ambiguity, with_clarification
from db_assistant.pipeline.generate import generate_sql
from db_assistant.pipeline.repair import EMPTY_RESULT, Attempt, repair_sql
from db_assistant.pipeline.schema_context import (
    DATE_RANGE_QUERY,
    VALUE_LIST_QUERIES,
    format_schema,
    tables_in_sql,
)
from db_assistant.pipeline.scope import IN_SCOPE, check_scope

ANSWERED = "answered"
DECLINED = "declined"  # out of scope or sensitive
NEEDS_CLARIFICATION = "needs_clarification"  # answer holds the follow-up question
FAILED = "failed"  # the query still failed after the last repair
ERROR = "error"  # something outside the pipeline failed; see error_kind

# error_kind values for status ERROR.
MISSING_API_KEY = "missing_api_key"
MCP_UNAVAILABLE = "mcp_unavailable"
LLM_ERROR = "llm_error"

# Step names reported to on_step and kept in Result.steps, in pipeline order.
STEP_SCOPE = "scope"
STEP_CLARIFY = "clarify"
STEP_GENERATE = "generate"
STEP_RUN = "run"  # detail: attempt number
STEP_REPAIR = "repair"  # detail: repair number
STEP_DESCRIBE_TABLE = "describe_table"  # detail: table name, called during a repair
STEP_ANSWER = "answer"

BROADENED_FALLBACK_NOTE = "The question was broadened because the original query returned no rows."


@dataclass
class Result:
    status: str
    answer: str
    reason: str | None = None  # the LLM's reason for a decline or clarification
    suggestions: list = field(default_factory=list)  # for a decline: questions it can answer
    sql: str | None = None  # the last SQL that was run
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    error: str | None = None  # technical detail for FAILED or ERROR
    error_kind: str | None = None  # for ERROR: MISSING_API_KEY, MCP_UNAVAILABLE or LLM_ERROR
    attempts: list = field(default_factory=list)  # every Attempt, in order
    scope_note: str | None = None  # set whenever a repair broadened the question
    data_note: str | None = None  # set when the question's data doesn't exist, with the date range
    steps: list = field(default_factory=list)  # (step, detail) pairs, in the order they ran

    @property
    def repair_count(self):
        return max(len(self.attempts) - 1, 0)


async def load_date_range(db):
    first, last = (await db.run_query(DATE_RANGE_QUERY))["rows"][0]
    return first, last


async def load_tables(db):
    """describe_table results for every allowed table."""
    return [await db.describe_table(name) for name in await db.list_tables()]


async def load_schema_text(db, tables=None):
    if tables is None:
        tables = await load_tables(db)
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


def no_data_note(date_range):
    return f"No data matched the original question. The data covers orders from {date_range[0]} to {date_range[1]}."


async def answer_question_async(question, llm, db, emit, clarification=None, first_sql=None):
    tables = await load_tables(db)
    schema_text = await load_schema_text(db, tables)
    date_range = await load_date_range(db)  # for data_note only, see schema_context.py

    if clarification is not None:
        # One clarification round only: the user has answered, so skip the
        # ambiguity check. The scope gate still runs on the combined text.
        question = with_clarification(question, *clarification)

    emit(STEP_SCOPE)
    scope = check_scope(llm, schema_text, question)
    if scope["decision"] != IN_SCOPE:
        return Result(
            status=DECLINED,
            answer=scope["message_to_user"],
            reason=scope["reason"],
            suggestions=scope.get("suggestions", []),
        )

    if clarification is None:
        emit(STEP_CLARIFY)
        clarity = check_ambiguity(llm, schema_text, question)
        if clarity["ambiguous"]:
            return Result(
                status=NEEDS_CLARIFICATION,
                answer=clarity["clarifying_question"],
                reason=clarity["reason"],
            )

    # first_sql is a debug hook (cli --first-sql) to force a repair.
    if first_sql:
        sql = first_sql
    else:
        emit(STEP_GENERATE)
        sql = generate_sql(llm, schema_text, question)
    attempts = []
    tool_calls, repair_reason, scope_note = [], None, None

    while True:
        emit(STEP_RUN, len(attempts) + 1)
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

        emit(STEP_REPAIR, len(attempts))
        repair = await repair_sql(llm, db, schema_text, question, attempts)
        # Reported after the repair returns, on this thread: the tool calls
        # themselves ran on a worker thread (see repair.py).
        for call in repair.tool_calls:
            emit(STEP_DESCRIBE_TABLE, call["input"].get("table_name"))
        sql, tool_calls, repair_reason = repair.sql, repair.tool_calls, repair.reason
        if repair.broadened_scope:
            scope_note = repair.note_to_user or BROADENED_FALLBACK_NOTE

    # The data doesn't exist (not just a wrong first query) if an attempt
    # came back empty and either nothing ever matched or the repair had to
    # broaden the question.
    had_empty = any(attempt.problem == EMPTY_RESULT for attempt in attempts)
    data_note = no_data_note(date_range) if had_empty and (scope_note or not result["rows"]) else None

    # The exact descriptions of the tables the final SQL used, from the schema
    # already loaded through MCP, so "describe X" answers state real columns.
    used = tables_in_sql(sql)
    used_tables = [table for table in tables if table["table"] in used]

    emit(STEP_ANSWER)
    return Result(
        status=ANSWERED,
        answer=write_answer(llm, question, sql, result, scope_note, data_note, used_tables),
        sql=sql,
        columns=result["columns"],
        rows=result["rows"],
        truncated=result["truncated"],
        attempts=attempts,
        scope_note=scope_note,
        data_note=data_note,
    )


def error_result(error):
    """A friendly ERROR result for a failure outside the pipeline's own logic."""
    if isinstance(error, LLMNotConfigured):
        kind = MISSING_API_KEY
        message = "I can't reach the language model: no valid Anthropic API key is configured."
    elif isinstance(error, mcp_client.ServerUnavailable):
        kind = MCP_UNAVAILABLE
        message = "I can't reach the database server right now, so I can't answer questions."
    elif isinstance(error, LLMRefusal):
        kind = LLM_ERROR
        message = "The language model declined to handle this request. Try rephrasing the question."
    else:
        kind = LLM_ERROR
        message = "The language model couldn't complete the request. Please try again in a moment."
    return Result(status=ERROR, answer=message, error=str(error), error_kind=kind)


def answer_question(question, llm=None, server=None, clarification=None, first_sql=None, on_step=None):
    """Answer one question. Blocking; opens and closes the MCP connection.

    llm: defaults to the real LLMClient. server: an MCPServer object to connect
    in-process (tests); None starts the real server process.
    clarification: (clarifying_question, user_answer) after a
    NEEDS_CLARIFICATION result; pass the original question again with it.
    first_sql: use this instead of the LLM's first SQL (debugging repairs).
    on_step(step, detail): called as each step starts (STEP_* names).

    Never raises for missing configuration or an unreachable LLM or MCP
    server: those come back as status ERROR.
    """
    steps = []

    def emit(step, detail=None):
        steps.append((step, detail))
        if on_step is not None:
            on_step(step, detail)

    def finish(result):
        result.steps = steps
        return result

    if llm is None:
        from db_assistant.llm.client import LLMClient

        try:
            llm = LLMClient()
        except LLMError as error:
            return finish(error_result(error))

    async def run():
        try:
            async with mcp_client.connect(server) as db:
                try:
                    return await answer_question_async(question, llm, db, emit, clarification, first_sql)
                except (LLMError, mcp_client.ServerUnavailable) as error:
                    return error_result(error)
        except mcp_client.ServerUnavailable as error:  # could not connect at all
            return error_result(error)

    return finish(anyio.run(run))


def list_allowed_tables(server=None):
    """The tables the MCP server allows, via its list_tables tool.

    Raises mcp_client.ServerUnavailable if the server can't be reached.
    """

    async def run():
        async with mcp_client.connect(server) as db:
            return await db.list_tables()

    return anyio.run(run)
