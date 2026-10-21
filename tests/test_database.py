"""Database layer: results, row limit, timeout and read-only access."""

import sqlite3
import time

import pytest

from db_assistant.mcp_server.database import QueryError, connect, run_query
from db_assistant.mcp_server.guardrails import QueryRejected


def test_returns_columns_and_rows():
    result = run_query("SELECT id, name, price FROM products WHERE id = 1")
    assert result.columns == ["id", "name", "price"]
    assert result.rows == [(1, "Sci-Fi Novel", 14.99)]
    assert result.truncated is False


def test_empty_result_is_not_an_error():
    result = run_query("SELECT * FROM customers WHERE country = 'GB'")
    assert result.rows == []
    assert result.columns == ["id", "name", "country", "signup_date"]


def test_row_limit_cuts_off_and_reports_truncation():
    # orders has 300 rows.
    result = run_query("SELECT id FROM orders", row_limit=50)
    assert len(result.rows) == 50
    assert result.truncated is True


def test_exactly_at_row_limit_is_not_truncated():
    result = run_query("SELECT id FROM products", row_limit=10)
    assert len(result.rows) == 10
    assert result.truncated is False


def test_runaway_query_stopped_by_timeout():
    # An endless recursive CTE. MAX() means no row is produced until it finishes,
    # so the row limit can't stop it; only the timeout can. It uses only
    # allowed actions (SELECT, RECURSIVE, FUNCTION), so the authorizer won't.
    sql = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
        "SELECT MAX(x) FROM n"
    )
    start = time.monotonic()
    with pytest.raises(QueryError, match="timed out after 0.5 seconds") as excinfo:
        run_query(sql, timeout_seconds=0.5)
    elapsed = time.monotonic() - start

    assert not isinstance(excinfo.value, QueryRejected)
    assert 0.5 <= elapsed < 3


def test_syntax_error_is_query_error():
    with pytest.raises(QueryError):
        run_query("SELECT FROM WHERE")


def test_connection_is_read_only():
    # Even with no guardrails at all, SQLite itself refuses to write.
    conn = connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM orders")
    finally:
        conn.close()
