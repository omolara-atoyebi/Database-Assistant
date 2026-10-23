"""Happy path: SQL generation, extraction and the end-to-end pipeline (offline).

End-to-end tests use the FakeLLM and connect to the real MCP server in-process,
so scripted SQL runs through the server's guardrails against the real data.
"""

import pytest

from db_assistant.mcp_server import database
from db_assistant.mcp_server.server import mcp
from db_assistant.pipeline.generate import build_system_prompt, extract_sql, generate_sql
from db_assistant.pipeline.orchestrator import answer_question

JULY_SQL = "SELECT COUNT(*) AS order_count FROM orders WHERE strftime('%Y-%m', order_date) = '2025-07'"

# The scope gate and clarification step run first; these let a question through.
IN_SCOPE = {"decision": "in_scope", "reason": "Aggregate over orders.", "message_to_user": ""}
CLEAR = {"ambiguous": False, "reason": "One clear meaning.", "clarifying_question": ""}


# --- extract_sql ------------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    [
        f"```sql\n{JULY_SQL};\n```",
        f"```\n{JULY_SQL}\n```",
        f"```SQL\n{JULY_SQL}\n```",
        f"Here is the query:\n\n```sql\n{JULY_SQL};\n```\n\nIt counts July orders.",
        f"{JULY_SQL};",
        f"  \n{JULY_SQL}\n  ",
    ],
    ids=["fenced", "fenced-no-tag", "fenced-uppercase-tag", "prose-around-fence", "unfenced", "whitespace"],
)
def test_extract_sql_returns_a_clean_query(reply):
    assert extract_sql(reply) == JULY_SQL


def test_extract_sql_keeps_multiline_queries_intact():
    sql = "SELECT country, COUNT(*)\nFROM customers\nGROUP BY country"
    assert extract_sql(f"```sql\n{sql}\n```") == sql


# --- generate_sql -----------------------------------------------------------

def test_generate_sql_sends_schema_and_question(fake_llm):
    llm = fake_llm([f"```sql\n{JULY_SQL}\n```"])

    sql = generate_sql(llm, "SCHEMA TEXT", "How many orders in July 2025?")

    assert sql == JULY_SQL
    call = llm.calls[0]
    assert call["system"] == build_system_prompt("SCHEMA TEXT")
    assert "SCHEMA TEXT" in call["system"]
    assert call["user_message"] == "How many orders in July 2025?"


# --- End to end through the MCP server --------------------------------------

def test_question_is_answered_with_rows_from_the_server_and_cited_sql(fake_llm):
    expected_count = database.run_query(JULY_SQL).rows[0][0]
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        f"```sql\n{JULY_SQL};\n```",
        f"There were {expected_count} orders in July 2025.",
    ])

    result = answer_question("How many orders were placed in July 2025?", llm=llm, server=mcp)

    assert result.status == "answered"
    assert result.error is None
    assert result.sql == JULY_SQL
    assert result.columns == ["order_count"]
    assert result.rows == [[expected_count]]
    assert result.answer == f"There were {expected_count} orders in July 2025."

    generate_call, answer_call = llm.calls[2:]
    # The SQL step saw the schema context fetched through MCP.
    assert "Table orders:" in generate_call["system"]
    assert "customers.country: DE, FI, FR, SE, UK, US" in generate_call["system"]
    # The answer step saw the question, the exact SQL and the real rows.
    assert "How many orders were placed in July 2025?" in answer_call["user_message"]
    assert JULY_SQL in answer_call["user_message"]
    assert f"order_count\n{expected_count}" in answer_call["user_message"]


def test_truncated_result_is_flagged_to_the_answer_step(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, "SELECT id FROM orders", "Based on the first rows only..."])

    result = answer_question("List all order ids", llm=llm, server=mcp)

    assert result.truncated is True
    assert "truncated" in llm.calls[3]["user_message"]


# Rejected or failing SQL now triggers self-repair: see tests/test_pipeline_repair.py.
