"""Builds the schema text the LLM sees before it writes SQL.

format_schema is a pure function: it takes table descriptions (the shape the
MCP describe_table tool returns) and value lists, and returns text. It does not
query the database itself; Phase 6 fetches its inputs through the MCP server.
"""

import re

from db_assistant import config
from db_assistant.pipeline.few_shot import FEW_SHOT_EXAMPLES

# Columns with a small fixed set of values, and the SQL that lists them.
VALUE_LIST_QUERIES = {
    "customers.country": "SELECT DISTINCT country FROM customers ORDER BY country",
    "products.category": "SELECT DISTINCT category FROM products ORDER BY category",
}

# The period the data covers. Used only for the answer's "no data" note; it is
# deliberately NOT in the schema context, because the scope gate would read
# an out-of-range date as "not in the database" and decline the question.
DATE_RANGE_QUERY = "SELECT MIN(order_date), MAX(order_date) FROM orders"

DATA_NOTES = [
    "Dates are TEXT in YYYY-MM-DD format. Use strftime('%Y-%m', order_date) or "
    "date(...) to bucket and compare them.",
    "orders.total_amount equals SUM(quantity * unit_price) of that order's "
    "order_items. Joining orders to order_items repeats total_amount once per "
    "item, so SUM(total_amount) over that join double-counts.",
    "For revenue, use order_items.quantity * order_items.unit_price (the price "
    "at sale), not products.price.",
    "Country codes are 2-letter; the United Kingdom is 'UK', not 'GB'.",
]


def format_table(table):
    lines = [f"Table {table['table']}:"]
    for column in table["columns"]:
        flags = []
        if column["primary_key"]:
            flags.append("primary key")
        if column["not_null"]:
            flags.append("not null")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"  - {column['name']} {column['type']}{suffix}")
    for fk in table["foreign_keys"]:
        lines.append(f"  - foreign key: {table['table']}.{fk['column']} -> {fk['references']}")
    return "\n".join(lines)


def tables_in_sql(sql, table_names=config.ALLOWED_TABLES):
    """The table names that appear as whole words in sql, in table_names order.

    Can over-include (e.g. an alias "AS orders" counts as the orders table),
    which is harmless where it's used; it can't miss a table the query reads,
    since every table read has to be named in the SQL.
    """
    return [name for name in table_names if re.search(rf"\b{re.escape(name)}\b", sql, re.IGNORECASE)]


def format_examples(examples=FEW_SHOT_EXAMPLES):
    blocks = []
    for number, example in enumerate(examples, start=1):
        blocks.append(
            f"Example {number} ({example['lesson']})\n"
            f"Question: {example['question']}\n"
            f"SQL:\n{example['sql']}"
        )
    return "\n\n".join(blocks)


def format_schema(tables, value_lists):
    """Return the full schema context: tables, known values, notes, examples.

    tables: list of describe_table results.
    value_lists: {"table.column": [values...]}.
    """
    sections = [
        "Database: SQLite (small e-commerce analytics dataset).",
        "\n\n".join(format_table(table) for table in tables),
        "Known values:\n"
        + "\n".join(f"  - {column}: {', '.join(values)}" for column, values in value_lists.items()),
        "Notes:\n" + "\n".join(f"  - {note}" for note in DATA_NOTES),
        "Examples:\n\n" + format_examples(),
    ]
    return "\n\n".join(sections)
