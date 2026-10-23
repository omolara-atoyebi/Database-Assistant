"""Turns query rows into a natural-language answer.

The LLM writes only the sentence. The cited SQL is attached by the
orchestrator, so the SQL shown is always exactly the SQL that ran.
"""

INSTRUCTIONS = """You answer questions about an e-commerce analytics database.

You are given the user's question, the SQL query that was run, and the rows it
returned. Answer the question in one to three plain sentences, using only those
rows. Do not invent numbers. Do not repeat the SQL. If the result was
truncated, say the answer is based on the first rows only. If the SQL has a
LIMIT the question did not ask for, say so, e.g. "Showing the top 10, since
no number was given." If a scope note is given, the query was broadened
because the original question had no matching data: start your answer by
telling the user that, using the note."""


def format_rows(result):
    """Render a run_query result as a small pipe-separated table."""
    lines = [" | ".join(result["columns"])]
    lines += [" | ".join(str(value) for value in row) for row in result["rows"]]
    return "\n".join(lines)


def build_user_message(question, sql, result, scope_note=None):
    parts = [
        f"Question: {question}",
        f"SQL:\n{sql}",
        f"Rows ({result['row_count']}):\n{format_rows(result)}",
    ]
    if result["truncated"]:
        parts.append(f"Note: the result was truncated to the first {result['row_count']} rows.")
    if scope_note:
        parts.append(f"Scope note: {scope_note}")
    return "\n\n".join(parts)


def write_answer(llm, question, sql, result, scope_note=None):
    return llm.complete(
        system=INSTRUCTIONS,
        user_message=build_user_message(question, sql, result, scope_note),
    ).strip()
