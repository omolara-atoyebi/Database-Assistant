"""Turns query rows into a natural-language answer.

The LLM writes only the sentence. The cited SQL is attached by the
orchestrator, so the SQL shown is always exactly the SQL that ran.
"""

from db_assistant.pipeline.schema_context import format_table

INSTRUCTIONS = """You answer questions about an e-commerce analytics database.

You are given the user's question, the SQL query that was run, and the rows it
returned. Answer the question in one to three plain sentences, using only those
rows. Do not invent numbers. Do not repeat the SQL. If the result was
truncated, say the answer is based on the first rows only. Mention a LIMIT
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


def format_rows(result):
    """Render a run_query result as a small pipe-separated table."""
    lines = [" | ".join(result["columns"])]
    lines += [" | ".join(str(value) for value in row) for row in result["rows"]]
    return "\n".join(lines)


def build_user_message(question, sql, result, scope_note=None, data_note=None, tables=()):
    parts = [
        f"Question: {question}",
        f"SQL:\n{sql}",
        f"Rows ({result['row_count']}):\n{format_rows(result)}",
    ]
    if tables:
        parts.append(
            "Table descriptions (from describe_table):\n"
            + "\n\n".join(format_table(table) for table in tables)
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
    return llm.complete(
        system=INSTRUCTIONS,
        user_message=build_user_message(question, sql, result, scope_note, data_note, tables),
    ).strip()
