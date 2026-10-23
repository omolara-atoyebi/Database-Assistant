"""The answer step gets the exact descriptions of the tables the final SQL used (offline).

The descriptions come from the schema each request already loads through the
MCP server, so a "describe the customers table" answer can state the real
columns, types and keys instead of guessing from sample rows.
"""

from db_assistant.mcp_server.server import mcp
from db_assistant.pipeline import answer
from db_assistant.pipeline.orchestrator import answer_question
from db_assistant.pipeline.schema_context import tables_in_sql

IN_SCOPE = {"decision": "in_scope", "reason": "About the data.", "message_to_user": "", "suggestions": []}
CLEAR = {"ambiguous": False, "reason": "One clear meaning.", "clarifying_question": ""}
SECTION = "Table descriptions (from describe_table):"


def fix(sql):
    return {"sql": sql, "reason": "Fixed.", "broadened_scope": False, "note_to_user": ""}


def answer_message(llm):
    return llm.calls[-1]["user_message"]


# --- tables_in_sql ------------------------------------------------------------

def test_tables_in_sql_finds_whole_word_table_names():
    sql = (
        "SELECT p.category, SUM(oi.quantity * oi.unit_price) FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id GROUP BY p.category"
    )
    # "order_items" does not count as "orders".
    assert tables_in_sql(sql) == ["products", "order_items"]


def test_tables_in_sql_ignores_case_and_returns_nothing_without_tables():
    assert tables_in_sql("select * from CUSTOMERS") == ["customers"]
    assert tables_in_sql("SELECT 1") == []


def test_tables_in_sql_may_over_include_an_alias_named_like_a_table():
    # Documented and harmless: an extra table description.
    assert tables_in_sql("SELECT COUNT(*) AS orders FROM customers") == ["customers", "orders"]


# --- What reaches the answer step ---------------------------------------------

def test_first_try_query_gets_the_exact_description_of_its_table(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, "SELECT * FROM customers LIMIT 10", "The customers table has ..."])

    answer_question("Describe the customers table", llm=llm, server=mcp)

    message = answer_message(llm)
    assert SECTION in message
    assert "Table customers:" in message
    assert "  - id INTEGER (primary key)" in message
    assert "  - signup_date TEXT" in message
    for other in ("orders", "products", "order_items"):
        assert f"Table {other}:" not in message


def test_join_gets_every_table_it_uses_with_foreign_keys(fake_llm):
    sql = (
        "SELECT c.country, COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY c.country"
    )
    llm = fake_llm([IN_SCOPE, CLEAR, sql, "..."])

    answer_question("Orders per country", llm=llm, server=mcp)

    message = answer_message(llm)
    assert "Table customers:" in message and "Table orders:" in message
    assert "foreign key: orders.customer_id -> customers.id" in message
    assert "Table products:" not in message


def test_after_a_repair_the_final_sql_tables_are_described_not_the_failed_ones(fake_llm):
    # The case from the UI: pragma_table_info is rejected, the repair queries customers.
    llm = fake_llm([
        IN_SCOPE,
        CLEAR,
        "SELECT name, type FROM pragma_table_info('orders')",
        fix("SELECT * FROM customers LIMIT 10"),
        "...",
    ])

    result = answer_question("Describe the customers table", llm=llm, server=mcp)

    assert result.repair_count == 1
    message = answer_message(llm)
    assert "Table customers:" in message
    assert "Table orders:" not in message


def test_query_without_tables_gets_no_descriptions(fake_llm):
    llm = fake_llm([IN_SCOPE, CLEAR, "SELECT 1 AS one", "..."])

    answer_question("What is one?", llm=llm, server=mcp)

    assert SECTION not in answer_message(llm)


def test_answer_instructions_limit_schema_talk_to_structure_questions():
    prompt = " ".join(answer.INSTRUCTIONS.split())
    assert "state them exactly from the descriptions, not from the rows" in prompt
    assert "Otherwise do not mention the schema" in prompt
