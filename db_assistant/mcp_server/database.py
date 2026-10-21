"""Run one SQL query against analytics.db, safely.

run_query opens the database read-only, applies the guardrails, enforces the
row limit and timeout, and returns a QueryResult. It raises:
- QueryRejected: blocked by a guardrail, or an unknown table/column.
- QueryError: the query failed while running (syntax error, timeout, ...).
"""

import sqlite3
import time
from dataclasses import dataclass

from db_assistant import config
from db_assistant.mcp_server.guardrails import (
    Authorizer,
    QueryRejected,
    allowed_tables_text,
    check_statement,
)

# How many SQLite steps run between timeout checks.
_PROGRESS_STEPS = 1000


class QueryError(Exception):
    """The query was allowed but failed while running."""


@dataclass
class QueryResult:
    columns: list
    rows: list
    truncated: bool


def connect():
    """Open analytics.db read-only. SQLite itself refuses any write."""
    return sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)


def run_query(
    sql,
    row_limit=config.ROW_LIMIT,
    timeout_seconds=config.QUERY_TIMEOUT_SECONDS,
):
    check_statement(sql)

    authorizer = Authorizer()
    deadline = time.monotonic() + timeout_seconds
    conn = connect()
    conn.set_authorizer(authorizer)
    # Returning True from the progress handler stops the query.
    conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_STEPS)

    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(row_limit + 1)
        columns = [description[0] for description in cursor.description]
    except sqlite3.ProgrammingError as error:
        if "one statement at a time" in str(error):
            raise QueryRejected(
                "Only one statement is allowed per query. " + allowed_tables_text()
            ) from error
        raise QueryError(str(error)) from error
    except sqlite3.DatabaseError as error:
        if authorizer.denial:
            raise QueryRejected(authorizer.denial) from error
        message = str(error)
        if message.startswith(("no such table", "no such column")):
            raise QueryRejected(f"{message}. " + allowed_tables_text()) from error
        if message == "interrupted" and time.monotonic() > deadline:
            raise QueryError(
                f"The query timed out after {timeout_seconds} seconds."
            ) from error
        raise QueryError(message) from error
    finally:
        conn.close()

    truncated = len(rows) > row_limit
    return QueryResult(columns=columns, rows=rows[:row_limit], truncated=truncated)
