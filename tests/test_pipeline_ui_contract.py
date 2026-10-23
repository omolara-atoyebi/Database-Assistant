"""What the UI relies on from the orchestrator (offline).

The UI holds no pipeline logic, so everything it shows must be on the Result:
steps, suggestions, friendly errors, repair count, notes, allowed tables.
"""

import pytest

from db_assistant import config
from db_assistant.llm import client as llm_client
from db_assistant.llm.client import LLMNotConfigured, LLMRefusal, LLMUnavailable
from db_assistant.mcp_server.server import mcp
from db_assistant.pipeline import mcp_client, orchestrator
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    DECLINED,
    ERROR,
    LLM_ERROR,
    MCP_UNAVAILABLE,
    MISSING_API_KEY,
    answer_question,
    list_allowed_tables,
)

IN_SCOPE = {"decision": "in_scope", "reason": "Aggregate over orders.", "message_to_user": "", "suggestions": []}
CLEAR = {"ambiguous": False, "reason": "One clear meaning.", "clarifying_question": ""}
TOTAL_SQL = "SELECT ROUND(SUM(total_amount), 2) AS revenue FROM orders"
DEC_2024_SQL = "SELECT id, order_date FROM orders WHERE strftime('%Y-%m', order_date) = '2024-12'"
RANGE_SQL = "SELECT MIN(order_date) AS first_order, MAX(order_date) AS last_order FROM orders"
FIRST_DATE, LAST_DATE = "2025-04-11", "2025-10-08"


def fix(sql, broadened_scope=False, note_to_user="", tool_calls=None):
    reply = {"sql": sql, "reason": "Fixed.", "broadened_scope": broadened_scope, "note_to_user": note_to_user}
    if tool_calls:
        reply["tool_calls"] = tool_calls
    return reply


class RaisingLLM:
    """An LLM whose every call raises the given error."""

    def __init__(self, error):
        self.error = error

    def complete(self, *args, **kwargs):
        raise self.error

    complete_json = complete_with_tools = complete


# --- Steps --------------------------------------------------------------------

def test_steps_are_reported_live_and_kept_on_the_result(fake_llm):
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT SUM(revenue) FROM orders",
        fix(TOTAL_SQL, tool_calls=[{"name": "describe_table", "input": {"table_name": "orders"}}]),
        "Total revenue is ...",
    ])
    reported = []

    result = answer_question(
        "What is the total revenue?",
        llm=llm,
        server=mcp,
        on_step=lambda step, detail: reported.append((step, detail)),
    )

    expected = [
        ("scope", None),
        ("clarify", None),
        ("generate", None),
        ("run", 1),
        ("repair", 1),
        ("describe_table", "orders"),
        ("run", 2),
        ("answer", None),
    ]
    assert reported == expected
    assert result.steps == expected


def test_steps_stop_where_the_pipeline_stops(fake_llm):
    declined = {**IN_SCOPE, "decision": "out_of_scope", "message_to_user": "No.", "suggestions": []}
    result = answer_question("What's the weather?", llm=fake_llm([declined]), server=mcp)
    assert result.steps == [("scope", None)]


# --- Declines carry suggestions -----------------------------------------------

def test_decline_carries_the_llm_suggestions(fake_llm):
    decision = {
        "decision": "sensitive",
        "reason": "One named customer.",
        "message_to_user": "I can't share one customer's orders.",
        "suggestions": ["What are the top 3 products by revenue?", "How many orders were placed per month?"],
    }

    result = answer_question("What did Bob 3 order?", llm=fake_llm([decision]), server=mcp)

    assert result.status == DECLINED
    assert result.suggestions == decision["suggestions"]


def test_scope_schema_requires_suggestions():
    from db_assistant.pipeline import scope

    assert "suggestions" in scope.SCHEMA["required"]
    assert scope.SCHEMA["properties"]["suggestions"]["type"] == "array"


# --- Friendly errors ----------------------------------------------------------

def test_missing_api_key_is_a_friendly_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_client, "load_dotenv", lambda *args, **kwargs: None)  # ignore .env

    result = answer_question("What is the total revenue?", server=mcp)

    assert result.status == ERROR
    assert result.error_kind == MISSING_API_KEY
    assert "API key" in result.answer
    assert "ANTHROPIC_API_KEY" in result.error
    assert ".env" in result.error


def test_llm_client_raises_its_own_error_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_client, "load_dotenv", lambda *args, **kwargs: None)
    with pytest.raises(LLMNotConfigured):
        llm_client.LLMClient()


@pytest.mark.parametrize(
    "error, kind",
    [
        (LLMNotConfigured("The API rejected the key."), MISSING_API_KEY),
        (LLMUnavailable("Could not reach the Anthropic API."), LLM_ERROR),
        (LLMRefusal("Declined."), LLM_ERROR),
    ],
    ids=["rejected-key", "api-unreachable", "refusal"],
)
def test_llm_failures_mid_request_are_friendly_errors(error, kind):
    result = answer_question("What is the total revenue?", llm=RaisingLLM(error), server=mcp)

    assert result.status == ERROR
    assert result.error_kind == kind
    assert result.answer  # a friendly message, not a traceback
    assert result.error == str(error)


def test_unreachable_mcp_server_is_a_friendly_error(fake_llm, monkeypatch):
    broken = mcp_client.StdioServerParameters(command="/nonexistent/python", args=[])
    monkeypatch.setattr(mcp_client, "server_params", lambda: broken)
    llm = fake_llm([])  # must not be called

    result = answer_question("What is the total revenue?", llm=llm)

    assert result.status == ERROR
    assert result.error_kind == MCP_UNAVAILABLE
    assert "database server" in result.answer
    assert llm.calls == []


def test_list_allowed_tables_raises_server_unavailable_when_unreachable(monkeypatch):
    broken = mcp_client.StdioServerParameters(command="/nonexistent/python", args=[])
    monkeypatch.setattr(mcp_client, "server_params", lambda: broken)
    with pytest.raises(mcp_client.ServerUnavailable):
        list_allowed_tables()


# --- Repair count, allowed tables ---------------------------------------------

def test_repair_count(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, "SELECT SUM(revenue) FROM orders", fix("SELECT nope FROM orders"),
                    fix(TOTAL_SQL), "Total ..."])
    result = answer_question("What is the total revenue?", llm=llm, server=mcp)
    assert result.repair_count == 2

    clean = answer_question("What is the total revenue?", llm=fake_llm([IN_SCOPE, CLEAR, TOTAL_SQL, "Total ..."]), server=mcp)
    assert clean.repair_count == 0


def test_list_allowed_tables_comes_from_the_mcp_server():
    assert list_allowed_tables(server=mcp) == list(config.ALLOWED_TABLES)


# --- Empty periods and broadened scope ------------------------------------------

def test_empty_period_answer_gets_a_no_data_note_with_the_date_range(fake_llm):
    # December 2024 is before the data starts; the repair broadens the question.
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        DEC_2024_SQL,
        fix(RANGE_SQL, broadened_scope=True, note_to_user="Showing the date range instead."),
        "There is no data for December 2024; the data covers ...",
    ])

    result = answer_question("List the orders placed in December 2024", llm=llm, server=mcp)

    assert result.status == ANSWERED
    # The note is computed in code from the real data, not written by the LLM.
    assert "No data matched" in result.data_note
    assert FIRST_DATE in result.data_note and LAST_DATE in result.data_note
    # The answer step is told, and its instructions say to start with it.
    answer_call = llm.calls[-1]
    assert f"Data note: {result.data_note}" in answer_call["user_message"]
    assert "date range the data covers" in " ".join(answer_call["system"].split())


def test_still_empty_after_repairs_also_gets_the_no_data_note(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, DEC_2024_SQL, fix(DEC_2024_SQL), fix(DEC_2024_SQL), "No orders then."])

    result = answer_question("List the orders placed in December 2024", llm=llm, server=mcp)

    assert result.rows == []
    assert FIRST_DATE in result.data_note and LAST_DATE in result.data_note


def test_no_data_note_when_a_repair_simply_fixes_the_filter(fake_llm):
    # 'GB' returns nothing, the repair corrects it to 'UK': the data exists,
    # so there is no "no data" note.
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT id FROM customers WHERE country = 'GB'",  # no rows
        fix("SELECT COUNT(*) FROM customers WHERE country = 'UK'"),
        "There are 6 customers in the UK.",
    ])

    result = answer_question("How many customers are in Great Britain?", llm=llm, server=mcp)

    assert result.repair_count == 1
    assert result.data_note is None


def test_broadened_repair_always_sets_a_scope_note_even_if_the_llm_left_it_empty(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, DEC_2024_SQL, fix(RANGE_SQL, broadened_scope=True, note_to_user=""), "..."])

    result = answer_question("List the orders placed in December 2024", llm=llm, server=mcp)

    assert result.scope_note == orchestrator.BROADENED_FALLBACK_NOTE
    assert f"Scope note: {result.scope_note}" in llm.calls[-1]["user_message"]


def test_date_range_is_not_shown_to_the_llm_steps(fake_llm):
    # Regression: with the range in the context, the scope gate declined
    # "List the orders placed in December 2024" as "not in the database".
    # Out-of-range dates must reach the query and the repair path instead.
    llm = fake_llm([IN_SCOPE, CLEAR, DEC_2024_SQL, fix(RANGE_SQL, broadened_scope=True), "..."])
    answer_question("List the orders placed in December 2024", llm=llm, server=mcp)
    scope_call, clarify_call, generate_call, repair_call, _ = llm.calls
    for call in (scope_call, clarify_call, generate_call, repair_call):
        assert FIRST_DATE not in call["system"]
        assert LAST_DATE not in call["system"]


def test_scope_prompt_keeps_empty_periods_in_scope():
    from db_assistant.pipeline import scope

    prompt = " ".join(scope.INSTRUCTIONS.split())
    assert "in_scope even if it may return no rows" in prompt
