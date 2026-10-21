"""Safety checks for SQL sent to run_query.

Two layers:
1. check_statement: a quick text check so obvious mistakes get a clear message.
2. Authorizer: SQLite calls it for every action a query takes. It allows only
   SELECT, READ on the allowed tables, FUNCTION and RECURSIVE. This is the
   real protection; it cannot be bypassed by how the SQL is written.

Every rejection message says what was denied and lists the allowed tables,
so the LLM can use it to repair the query.
"""

import re
import sqlite3

from db_assistant import config

# Optional leading comments, then SELECT or WITH as the first keyword.
_STARTS_WITH_SELECT = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(select|with)\b",
    re.IGNORECASE | re.DOTALL,
)

# Readable names for actions the authorizer is most likely to deny.
_ACTION_NAMES = {
    sqlite3.SQLITE_INSERT: "INSERT",
    sqlite3.SQLITE_UPDATE: "UPDATE",
    sqlite3.SQLITE_DELETE: "DELETE",
    sqlite3.SQLITE_PRAGMA: "PRAGMA",
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_CREATE_TABLE: "CREATE TABLE",
    sqlite3.SQLITE_DROP_TABLE: "DROP TABLE",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE",
    sqlite3.SQLITE_TRANSACTION: "TRANSACTION",
}

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


class QueryRejected(Exception):
    """The query was blocked by a guardrail or refers to something not allowed."""


def allowed_tables_text():
    return "Allowed tables: " + ", ".join(config.ALLOWED_TABLES) + "."


def check_statement(sql):
    """Raise QueryRejected unless sql looks like a single SELECT/WITH query."""
    if not sql or not sql.strip():
        raise QueryRejected("The query is empty. " + allowed_tables_text())
    if not _STARTS_WITH_SELECT.match(sql):
        first_word = sql.strip().split()[0].upper()
        raise QueryRejected(
            f"Only SELECT queries are allowed; this query starts with "
            f"{first_word}. " + allowed_tables_text()
        )


class Authorizer:
    """SQLite authorizer callback that records why it denied an action."""

    def __init__(self, allowed_tables=config.ALLOWED_TABLES):
        self.allowed_tables = set(allowed_tables)
        self.denial = None

    def __call__(self, action, arg1, arg2, db_name, trigger):
        if action not in _ALLOWED_ACTIONS:
            name = _ACTION_NAMES.get(action, f"action code {action}")
            target = f" on '{arg1}'" if arg1 else ""
            self.denial = (
                f"Only SELECT queries are allowed; this query tried {name}"
                f"{target}. " + allowed_tables_text()
            )
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and arg1 not in self.allowed_tables:
            self.denial = (
                f"Access denied: table '{arg1}' is not allowed. "
                + allowed_tables_text()
            )
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
