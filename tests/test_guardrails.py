"""Guardrails: unsafe SQL is rejected with a useful message, safe SQL is allowed."""

import sqlite3

import pytest

from db_assistant import config
from db_assistant.mcp_server.database import connect, run_query
from db_assistant.mcp_server.guardrails import Authorizer, QueryRejected

ALLOWED_TABLES_TEXT = "Allowed tables: customers, products, orders, order_items."


def assert_rejected(sql, *expected_in_message):
    """Check run_query rejects sql, naming what was denied and the allowed tables."""
    with pytest.raises(QueryRejected) as excinfo:
        run_query(sql)
    message = str(excinfo.value)
    assert ALLOWED_TABLES_TEXT in message
    for text in expected_in_message:
        assert text in message


# --- Rejected ---------------------------------------------------------------

def test_drop_rejected():
    assert_rejected("DROP TABLE orders", "DROP")


def test_delete_rejected():
    assert_rejected("DELETE FROM orders", "DELETE")


def test_update_rejected():
    assert_rejected("UPDATE products SET price = 0", "UPDATE")


def test_insert_rejected():
    assert_rejected("INSERT INTO products VALUES (99, 'x', 'Books', 1.0)", "INSERT")


def test_attach_rejected():
    assert_rejected("ATTACH DATABASE ':memory:' AS other", "ATTACH")


def test_pragma_rejected():
    assert_rejected("PRAGMA table_info(orders)", "PRAGMA")


def test_two_statements_rejected():
    assert_rejected("SELECT 1; DROP TABLE orders", "one statement")


def test_unknown_table_rejected():
    assert_rejected("SELECT * FROM invoices", "invoices")


def test_unknown_column_rejected():
    assert_rejected("SELECT discount FROM orders", "discount")


def test_sqlite_master_rejected():
    assert_rejected("SELECT name FROM sqlite_master", "'sqlite_master' is not allowed")


def test_write_hidden_behind_with_rejected_by_authorizer():
    # Starts with WITH, so it passes the quick text check; the authorizer stops it.
    assert_rejected("WITH x AS (SELECT 1) DELETE FROM orders", "DELETE", "'orders'")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE orders",
        "DELETE FROM orders",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE ':memory:' AS other",
        "SELECT name FROM sqlite_master",
    ],
)
def test_authorizer_alone_blocks_unsafe_sql(sql):
    # Skip the quick text check to prove the authorizer is a real second layer.
    authorizer = Authorizer()
    conn = connect()
    conn.set_authorizer(authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    finally:
        conn.close()
    # SQLite's own error wording varies; the authorizer's recorded reason is what we rely on.
    assert ALLOWED_TABLES_TEXT in authorizer.denial


# --- Allowed ----------------------------------------------------------------

def test_simple_select_allowed():
    result = run_query("SELECT id, name FROM products")
    assert len(result.rows) == 10


def test_strftime_and_sum_allowed():
    result = run_query(
        "SELECT strftime('%Y-%m', order_date) AS month, SUM(total_amount) "
        "FROM orders GROUP BY month ORDER BY month"
    )
    assert result.rows[0][0] == "2025-04"


def test_with_query_allowed():
    result = run_query(
        "WITH big AS (SELECT id FROM orders WHERE total_amount > 100) "
        "SELECT COUNT(*) FROM big"
    )
    assert result.rows[0][0] > 0


def test_recursive_with_allowed():
    result = run_query(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
        "SELECT x FROM n"
    )
    assert [row[0] for row in result.rows] == [1, 2, 3, 4, 5]


def test_join_across_all_tables_allowed():
    result = run_query(
        "SELECT c.country, p.category, SUM(oi.quantity * oi.unit_price) "
        "FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id "
        "JOIN customers c ON c.id = o.customer_id "
        "JOIN products p ON p.id = oi.product_id "
        "GROUP BY c.country, p.category"
    )
    assert len(result.rows) > 0


def test_leading_comment_allowed():
    result = run_query("-- count products\nSELECT COUNT(*) FROM products")
    assert result.rows == [(10,)]


def test_allowed_tables_text_matches_config():
    assert ALLOWED_TABLES_TEXT == "Allowed tables: " + ", ".join(config.ALLOWED_TABLES) + "."
