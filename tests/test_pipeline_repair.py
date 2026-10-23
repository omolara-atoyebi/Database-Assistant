"""Self-repair loop with describe_table tool use (offline).

The FakeLLM scripts bad SQL and then fixed SQL. All SQL, good or bad, runs
through the real MCP server in-process, so its guardrails decide what fails.
"""

import json

import pytest

from db_assistant import config
from db_assistant.mcp_server import database
from db_assistant.mcp_server.server import mcp
from db_assistant.pipeline import repair
from db_assistant.pipeline.orchestrator import ANSWERED, FAILED, answer_question

IN_SCOPE = {"decision": "in_scope", "reason": "Aggregate over orders.", "message_to_user": ""}
CLEAR = {"ambiguous": False, "reason": "One clear meaning.", "clarifying_question": ""}

TOTAL_SQL = "SELECT ROUND(SUM(total_amount), 2) AS revenue FROM orders"
DEC_2024_SQL = "SELECT id, order_date FROM orders WHERE strftime('%Y-%m', order_date) = '2024-12'"
RANGE_SQL = "SELECT MIN(order_date) AS first_order, MAX(order_date) AS last_order FROM orders"


def fix(sql, reason="Fixed the query.", broadened_scope=False, note_to_user="", tool_calls=None):
    """A scripted repair reply."""
    reply = {
        "sql": sql,
        "reason": reason,
        "broadened_scope": broadened_scope,
        "note_to_user": note_to_user,
    }
    if tool_calls:
        reply["tool_calls"] = tool_calls
    return reply


def repair_calls(llm):
    return [call for call in llm.calls if "tools" in call]


# --- Each kind of problem triggers a repair ---------------------------------

@pytest.mark.parametrize(
    "bad_sql, expected_in_problem",
    [
        ("SELECT SUM(revenue) FROM orders", "Rejected:"),  # validation failure
        ("SELECT * FROM invoices", "invoices"),  # validation failure
        ("SELECT SUM(total_amount FROM orders", "Query failed:"),  # execution error
    ],
    ids=["unknown-column", "unknown-table", "syntax-error"],
)
def test_failed_query_is_repaired_and_answered(fake_llm, bad_sql, expected_in_problem):
    llm = fake_llm([IN_SCOPE, CLEAR, bad_sql, fix(TOTAL_SQL), "Total revenue is ..."])

    result = answer_question("What is the total revenue?", llm=llm, server=mcp)

    assert result.status == ANSWERED
    assert result.sql == TOTAL_SQL
    assert result.rows == [[database.run_query(TOTAL_SQL).rows[0][0]]]
    first, repaired = result.attempts
    assert first.sql == bad_sql
    assert expected_in_problem in first.problem
    assert repaired.problem is None
    # The repair saw the failed SQL and the server's exact message.
    (repair_call,) = repair_calls(llm)
    assert bad_sql in repair_call["user_message"]
    assert first.problem in repair_call["user_message"]


def test_empty_result_is_repaired_and_the_broadened_scope_is_reported(fake_llm):
    note = "There are no orders in December 2024; the data covers 2025-04-11 to 2025-10-08."
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        DEC_2024_SQL,
        fix(RANGE_SQL, broadened_scope=True, note_to_user=note),
        "No orders in December 2024. The data covers ...",
    ])

    result = answer_question("List the orders placed in December 2024", llm=llm, server=mcp)

    assert result.status == ANSWERED
    assert result.attempts[0].problem == repair.EMPTY_RESULT
    assert repair.EMPTY_RESULT in repair_calls(llm)[0]["user_message"]
    assert result.rows == [["2025-04-11", "2025-10-08"]]
    assert result.scope_note == note
    # The answer step is told the question was broadened.
    assert f"Scope note: {note}" in llm.calls[-1]["user_message"]


def test_repair_prompt_says_to_check_types_before_broadening():
    prompt = repair.INSTRUCTIONS
    assert prompt.index("check the types and formats") < prompt.index("broaden the scope")


# --- The loop stops after MAX_REPAIRS ---------------------------------------

def test_loop_stops_after_two_repairs_with_an_honest_failure(fake_llm):
    assert config.MAX_REPAIRS == 2
    bad = "SELECT SUM(revenue) FROM orders"
    # No third repair and no answer step are scripted: the FakeLLM would
    # raise if the pipeline asked for either.
    llm = fake_llm([IN_SCOPE, CLEAR, bad, fix(bad), fix("SELECT nope FROM orders")])

    result = answer_question("What is the total revenue?", llm=llm, server=mcp)

    assert result.status == FAILED
    assert "couldn't answer" in result.answer
    assert len(result.attempts) == 3  # the first attempt + 2 repairs
    assert len(repair_calls(llm)) == 2
    assert "Rejected:" in result.error
    assert "nope" in result.error


def test_each_repair_sees_all_earlier_attempts(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT SUM(revenue) FROM orders",
        fix("SELECT SUM(amount) FROM orders"),
        fix(TOTAL_SQL),
        "Total revenue is ...",
    ])

    answer_question("What is the total revenue?", llm=llm, server=mcp)

    second_repair = repair_calls(llm)[1]["user_message"]
    assert "Attempt 1:" in second_repair and "SUM(revenue)" in second_repair
    assert "Attempt 2:" in second_repair and "SUM(amount)" in second_repair


def test_still_empty_after_two_repairs_is_answered_as_no_match(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        DEC_2024_SQL,
        fix(DEC_2024_SQL),
        fix(DEC_2024_SQL),
        "There were no orders in December 2024.",
    ])

    result = answer_question("List the orders placed in December 2024", llm=llm, server=mcp)

    assert result.status == ANSWERED
    assert result.rows == []
    assert len(result.attempts) == 3


# --- describe_table tool use, only inside repair ----------------------------

def test_repair_can_call_describe_table_through_the_mcp_server(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT SUM(revenue) FROM orders",
        fix(TOTAL_SQL, tool_calls=[{"name": "describe_table", "input": {"table_name": "orders"}}]),
        "Total revenue is ...",
    ])

    result = answer_question("What is the total revenue?", llm=llm, server=mcp)

    (repair_call,) = repair_calls(llm)
    (outcome,) = repair_call["tool_outcomes"]
    assert outcome["is_error"] is False
    columns = [column["name"] for column in json.loads(outcome["content"])["columns"]]
    assert columns == ["id", "customer_id", "order_date", "total_amount"]
    # The call is recorded on the attempt it helped write.
    assert result.attempts[1].tool_calls == [
        {"name": "describe_table", "input": {"table_name": "orders"}, "is_error": False}
    ]


def test_describe_table_rejection_goes_back_to_the_llm_as_a_tool_error(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT SUM(revenue) FROM orders",
        fix(TOTAL_SQL, tool_calls=[{"name": "describe_table", "input": {"table_name": "sqlite_master"}}]),
        "Total revenue is ...",
    ])

    answer_question("What is the total revenue?", llm=llm, server=mcp)

    (outcome,) = repair_calls(llm)[0]["tool_outcomes"]
    assert outcome["is_error"] is True
    assert "Rejected:" in outcome["content"]
    assert "Allowed tables:" in outcome["content"]


def test_describe_table_is_offered_only_inside_repair(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT SUM(revenue) FROM orders",
        fix(TOTAL_SQL),
        "Total revenue is ...",
    ])

    answer_question("What is the total revenue?", llm=llm, server=mcp)

    scope_call, clarify_call, generate_call, repair_call, answer_call = llm.calls
    for call in (scope_call, clarify_call, generate_call, answer_call):
        assert "tools" not in call
    assert repair_call["tools"] == [repair.DESCRIBE_TABLE_TOOL]
    assert repair_call["schema"] is repair.SCHEMA


def test_no_repair_when_the_first_query_works(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, TOTAL_SQL, "Total revenue is ..."])

    result = answer_question("What is the total revenue?", llm=llm, server=mcp)

    assert repair_calls(llm) == []
    assert len(result.attempts) == 1


# --- --first-sql debug hook -------------------------------------------------

def test_first_sql_replaces_the_llm_first_attempt(fake_llm):
    # No generate reply scripted: the pipeline must use first_sql instead.
    llm = fake_llm([IN_SCOPE, CLEAR, fix(TOTAL_SQL), "Total revenue is ..."])

    result = answer_question(
        "What is the total revenue?",
        llm=llm,
        server=mcp,
        first_sql="SELECT SUM(revenue) FROM orders",
    )

    assert result.status == ANSWERED
    assert result.attempts[0].sql == "SELECT SUM(revenue) FROM orders"
    assert "Rejected:" in result.attempts[0].problem
    assert result.sql == TOTAL_SQL
