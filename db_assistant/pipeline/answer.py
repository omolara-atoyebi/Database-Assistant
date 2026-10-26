"""Turns query rows into a natural-language answer.

The LLM writes only the sentence. The cited SQL is attached by the
orchestrator, so the SQL shown is always exactly the SQL that ran.
"""

from db_assistant import config
from db_assistant.pipeline.schema_context import format_table

INSTRUCTIONS = """You answer questions about an e-commerce analytics database.

You are given the user's question, the SQL query that was run, and the rows it
returned. Answer the question in one to three plain sentences, using only those
rows. Do not invent numbers. Do not repeat the SQL. If a note says you see
only some of the rows, or that the result was truncated, say the answer is
based on the first rows only, and do not state totals, counts, averages,
highest/lowest or "all"/"none" claims that would need rows you weren't shown. Mention a LIMIT
only when the question gave no number, e.g. "Showing the top 10, since no
number was given."; if the question gave a number, do not comment on the
LIMIT. If a data note is given, the original question had
no matching data: start your answer by saying so and give the date range the
data covers, as in the note. If a scope note is given, the query was
broadened: say what you show instead, using the note.

Table descriptions give the exact columns, types and keys of the tables the
query used. If the question is about the structure of a table (its columns,
types or keys), state them exactly from the descriptions, not from the rows.
Otherwise do not mention the schema: no table names, column types or keys."""


def format_rows(result, limit=None):
    """Render a run_query result as a small pipe-separated table (first `limit` rows)."""
    lines = [" | ".join(result["columns"])]
    lines += [" | ".join(str(value) for value in row) for row in result["rows"][:limit]]
    return "\n".join(lines)


def build_user_message(question, sql, result, scope_note=None, data_note=None, tables=()):
    # Only the first ANSWER_ROW_LIMIT rows are sent, to keep the prompt small.
    shown = min(result["row_count"], config.ANSWER_ROW_LIMIT)
    parts = [
        f"Question: {question}",
        f"SQL:\n{sql}",
        f"Rows (showing {shown} of {result['row_count']}):\n{format_rows(result, shown)}",
    ]
    if tables:
        parts.append(
            "Table descriptions (from describe_table):\n"
            + "\n\n".join(format_table(table) for table in tables)
        )
    if shown < result["row_count"]:
        parts.append(
            f"Note: the query returned {result['row_count']} rows, but you are shown only "
            f"the first {shown}. Base the answer on these rows and say so."
        )
    if result["truncated"]:
        parts.append(f"Note: the result was truncated to the first {result['row_count']} rows.")
    if data_note:
        parts.append(f"Data note: {data_note}")
    if scope_note:
        parts.append(f"Scope note: {scope_note}")
    return "\n\n".join(parts)


def write_answer(llm, question, sql, result, scope_note=None, data_note=None, tables=()):
    """tables: describe_table results for the tables the SQL used."""
    # No thinking: summarising the rows is quick, and the user is waiting.
    return llm.complete(
        system=INSTRUCTIONS,
        user_message=build_user_message(question, sql, result, scope_note, data_note, tables),
        think=False,
    ).strip()
