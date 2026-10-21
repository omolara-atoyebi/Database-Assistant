"""Schema context and few-shot examples (offline; no API calls)."""

import pytest

from db_assistant import config
from db_assistant.mcp_server import database
from db_assistant.mcp_server.server import describe_table
from db_assistant.pipeline.few_shot import FEW_SHOT_EXAMPLES
from db_assistant.pipeline.schema_context import VALUE_LIST_QUERIES, format_schema


@pytest.fixture(scope="module")
def schema_text():
    # Phase 6 will fetch these through the MCP client; here we call the same
    # server functions directly.
    tables = [describe_table(name) for name in config.ALLOWED_TABLES]
    value_lists = {
        column: [row[0] for row in database.run_query(sql).rows]
        for column, sql in VALUE_LIST_QUERIES.items()
    }
    return format_schema(tables, value_lists)


# --- Schema text ------------------------------------------------------------

def test_schema_includes_all_four_tables(schema_text):
    for table in config.ALLOWED_TABLES:
        assert f"Table {table}:" in schema_text


def test_schema_includes_foreign_keys(schema_text):
    assert "orders.customer_id -> customers.id" in schema_text
    assert "order_items.order_id -> orders.id" in schema_text
    assert "order_items.product_id -> products.id" in schema_text


def test_schema_includes_value_lists_from_the_data(schema_text):
    assert "customers.country: DE, FI, FR, SE, UK, US" in schema_text
    assert "products.category: Books, Clothing, Electronics, Games, Home" in schema_text


def test_schema_includes_the_query_notes(schema_text):
    assert "YYYY-MM-DD" in schema_text
    assert "double-counts" in schema_text
    assert "unit_price" in schema_text
    assert "'UK', not 'GB'" in schema_text


def test_schema_includes_all_five_examples(schema_text):
    assert len(FEW_SHOT_EXAMPLES) == 5
    for example in FEW_SHOT_EXAMPLES:
        assert example["question"] in schema_text
        assert example["sql"] in schema_text


# --- Few-shot examples ------------------------------------------------------

@pytest.mark.parametrize("example", FEW_SHOT_EXAMPLES, ids=lambda e: e["question"][:40])
def test_example_sql_passes_guardrails_and_returns_rows(example):
    result = database.run_query(example["sql"])
    assert result.rows, f"No rows for: {example['question']}"


def test_example_five_joins_all_four_tables():
    sql = FEW_SHOT_EXAMPLES[4]["sql"]
    for table in config.ALLOWED_TABLES:
        assert table in sql


def test_double_counting_trap_is_real():
    # The naive join must give a different (inflated) answer than example 2,
    # otherwise the example teaches nothing.
    naive = database.run_query(
        "SELECT ROUND(SUM(o.total_amount), 2) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id "
        "WHERE p.category = 'Electronics'"
    ).rows[0][0]
    correct = database.run_query(FEW_SHOT_EXAMPLES[1]["sql"]).rows[0][0]
    assert naive > correct


# --- FakeLLM ----------------------------------------------------------------

def test_fake_llm_returns_scripted_replies_and_records_calls(fake_llm):
    llm = fake_llm(["first", "second"])
    assert llm.complete("sys", "q1") == "first"
    assert llm.complete("sys", "q2") == "second"
    assert [call["user_message"] for call in llm.calls] == ["q1", "q2"]
