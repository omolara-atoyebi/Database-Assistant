"""Asks the LLM for SQL and extracts a clean query from its reply."""

import re

from db_assistant import config

INSTRUCTIONS = f"""You write SQLite queries for a data analytics assistant.

Write exactly one read-only SELECT (or WITH ... SELECT) query that answers the
user's question, using only the tables and columns below. Follow the notes and
examples; they cover mistakes that are easy to make with this data.

If the question asks for a ranking ("top", "best", "most", ...) without giving
a number, use LIMIT {config.DEFAULT_TOP_N}.

Reply with only the SQL, in a ```sql code block."""

# A fenced code block, with or without a language tag: ```sql ... ``` or ``` ... ```
FENCED_BLOCK = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


def build_system_prompt(schema_text):
    return f"{INSTRUCTIONS}\n\n{schema_text}"


def extract_sql(reply):
    """Return the query from an LLM reply.

    Uses the first fenced code block if there is one (prose around it is
    ignored); otherwise treats the whole reply as SQL. Strips whitespace and a
    trailing semicolon.
    """
    match = FENCED_BLOCK.search(reply)
    sql = match.group(1) if match else reply
    return sql.strip().rstrip(";").strip()


def generate_sql(llm, schema_text, question):
    reply = llm.complete(system=build_system_prompt(schema_text), user_message=question)
    return extract_sql(reply)
