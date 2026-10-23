"""Scope gate and clarification routing (offline).

The FakeLLM returns scripted decisions; these tests check that the pipeline
routes on them correctly. Whether the real LLM decides well is checked by hand
with the CLI (see docs/PLAN.md, Phase 7).
"""

from db_assistant import config
from db_assistant.mcp_server.server import mcp
from db_assistant.pipeline import answer, clarify, generate, scope
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    DECLINED,
    NEEDS_CLARIFICATION,
    answer_question,
)

IN_SCOPE = {"decision": "in_scope", "reason": "Aggregate over orders.", "message_to_user": ""}
CLEAR = {"ambiguous": False, "reason": "One clear meaning.", "clarifying_question": ""}
OUT_OF_SCOPE = {
    "decision": "out_of_scope",
    "reason": "Weather is not in the database.",
    "message_to_user": "I can only answer questions about the shop's orders, products and customers.",
}
SENSITIVE = {
    "decision": "sensitive",
    "reason": "Asks for one named customer's orders.",
    "message_to_user": "I can't share an individual customer's data, but I can give totals.",
}
AMBIGUOUS = {
    "ambiguous": True,
    "reason": "'Best' could mean revenue or order count.",
    "clarifying_question": "Do you mean best by total revenue or by number of orders?",
}

COUNT_SQL = "SELECT COUNT(*) FROM customers WHERE country = 'DE'"


# --- Declines ---------------------------------------------------------------

def test_out_of_scope_question_is_declined_with_the_llm_message(fake_llm):
    llm = fake_llm([OUT_OF_SCOPE])

    result = answer_question("What's the weather in Berlin?", llm=llm, server=mcp)

    assert result.status == DECLINED
    assert result.answer == OUT_OF_SCOPE["message_to_user"]
    assert result.reason == OUT_OF_SCOPE["reason"]
    assert result.sql is None
    assert len(llm.calls) == 1  # no clarification, no SQL


def test_sensitive_question_is_declined_with_the_llm_message(fake_llm):
    llm = fake_llm([SENSITIVE])

    result = answer_question("What did Bob 3 order?", llm=llm, server=mcp)

    assert result.status == DECLINED
    assert result.answer == SENSITIVE["message_to_user"]
    assert result.sql is None
    assert len(llm.calls) == 1


# --- Clarification ----------------------------------------------------------

def test_ambiguous_question_returns_the_llm_follow_up_question(fake_llm):
    llm = fake_llm([IN_SCOPE, AMBIGUOUS])

    result = answer_question("Who are our best customers?", llm=llm, server=mcp)

    assert result.status == NEEDS_CLARIFICATION
    assert result.answer == AMBIGUOUS["clarifying_question"]
    assert result.reason == AMBIGUOUS["reason"]
    assert result.sql is None
    assert len(llm.calls) == 2  # no SQL generated


def test_answered_clarification_skips_the_ambiguity_check(fake_llm):
    sql = "SELECT customer_id, COUNT(*) AS orders FROM orders GROUP BY customer_id ORDER BY orders DESC LIMIT 3"
    # Scope, then straight to SQL and answer: no clarify decision is scripted.
    llm = fake_llm([IN_SCOPE, sql, "The top 3 customers by order count are ..."])

    result = answer_question(
        "Who are our best customers?",
        llm=llm,
        server=mcp,
        clarification=(AMBIGUOUS["clarifying_question"], "by number of orders"),
    )

    assert result.status == ANSWERED
    assert result.sql == sql
    scope_call, generate_call, _ = llm.calls
    # Both the scope gate and the SQL step saw the user's answer.
    for call in (scope_call, generate_call):
        assert "Who are our best customers?" in call["user_message"]
        assert "by number of orders" in call["user_message"]


def test_sensitive_answer_to_a_clarification_is_still_declined(fake_llm):
    llm = fake_llm([SENSITIVE])

    result = answer_question(
        "Who are our best customers?",
        llm=llm,
        server=mcp,
        clarification=(AMBIGUOUS["clarifying_question"], "show me Bob 3's orders"),
    )

    assert result.status == DECLINED


# --- Clear, in-scope question -----------------------------------------------

def test_clear_in_scope_question_is_answered(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, COUNT_SQL, "There are N customers in Germany."])

    result = answer_question("How many customers are in Germany?", llm=llm, server=mcp)

    assert result.status == ANSWERED
    assert result.sql == COUNT_SQL
    assert len(result.rows) == 1


# --- The decisions are structured LLM calls ---------------------------------

def test_scope_and_clarify_use_structured_output_with_a_reason(fake_llm):
    llm = fake_llm([IN_SCOPE, AMBIGUOUS])

    answer_question("Who are our best customers?", llm=llm, server=mcp)

    scope_call, clarify_call = llm.calls
    assert scope_call["schema"] is scope.SCHEMA
    assert clarify_call["schema"] is clarify.SCHEMA
    assert scope.SCHEMA["properties"]["decision"]["enum"] == ["in_scope", "out_of_scope", "sensitive"]
    for schema in (scope.SCHEMA, clarify.SCHEMA):
        assert "reason" in schema["required"]
        assert schema["additionalProperties"] is False
    # Both decisions are made with the schema in view.
    for call in (scope_call, clarify_call):
        assert "Table customers:" in call["system"]
        assert call["user_message"] == "Who are our best customers?"


# --- Sensitivity policy and default limit (prompt content) -------------------

def test_scope_prompt_states_the_policy_with_examples_on_both_sides():
    prompt = scope.INSTRUCTIONS
    # Rankings of named customers are in scope, including the bug-report case.
    assert "Who are our top 3 customers by revenue?" in prompt
    assert "Best customers, by total revenue" in prompt
    # One specific customer is sensitive.
    assert "What did Bob 3 order?" in prompt
    assert "How much has Bob 3 spent in total?" in prompt
    # The decision must not depend on how the follow-up is answered.
    assert "must not change" in prompt


def test_sql_prompt_uses_the_default_limit_for_rankings_without_a_number():
    assert f"LIMIT {config.DEFAULT_TOP_N}" in generate.INSTRUCTIONS


def test_answer_prompt_says_to_mention_a_default_limit():
    prompt = " ".join(answer.INSTRUCTIONS.split())
    assert "Mention a LIMIT only when the question gave no number" in prompt
    assert "if the question gave a number, do not comment on the LIMIT" in prompt
