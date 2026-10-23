"""Clarification: the LLM decides whether a question is ambiguous and, if so,
writes the follow-up question for the user. An LLM judgment, not a rule."""

INSTRUCTIONS = """You check questions for a data analytics assistant before it writes SQL.

Decide whether the user's question is ambiguous: could it reasonably be
answered with different SQL that gives meaningfully different results?
Example: "Who are our best customers?" could mean by revenue or by number of
orders. Do not ask about things with a sensible default (e.g. "revenue"
means quantity * unit_price over all orders; no date range means all data).

Give a short reason. If the question is ambiguous, write clarifying_question:
one short question to the user, offering the likely options. Otherwise leave
clarifying_question empty.

The database schema is below."""

SCHEMA = {
    "type": "object",
    "properties": {
        "ambiguous": {"type": "boolean"},
        "reason": {"type": "string"},
        "clarifying_question": {"type": "string"},
    },
    "required": ["ambiguous", "reason", "clarifying_question"],
    "additionalProperties": False,
}


def check_ambiguity(llm, schema_text, question):
    """Return {"ambiguous", "reason", "clarifying_question"}."""
    return llm.complete_json(
        system=f"{INSTRUCTIONS}\n\n{schema_text}",
        user_message=question,
        schema=SCHEMA,
    )


def with_clarification(question, clarifying_question, user_answer):
    """Combine the original question and the user's answer into one question."""
    return (
        f"{question}\n"
        f"(Clarification. Asked: {clarifying_question} User answered: {user_answer})"
    )
