"""Scope gate: the LLM decides whether a question is in scope, out of scope or sensitive.

This is an LLM judgment with a reason (structured output), not a keyword rule.
"""

IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"
SENSITIVE = "sensitive"

INSTRUCTIONS = """You are the scope gate for a data analytics assistant.

The assistant answers questions by querying a small e-commerce database:
customers, products, orders and order items (schema below). Classify the
user's question as one of:

- in_scope: it can be answered from this database and is not sensitive.
- out_of_scope: it is not about this data (weather, general knowledge,
  coding help, ...) or asks for something the database does not contain.
  A question about this data is in_scope even if it may return no rows,
  e.g. a period with no orders: that is handled later, when the query runs.
- sensitive: it targets one specific customer, named or identified, and
  asks for their details or records.

Sensitivity policy. The line is whether the question picks out one person:
- Rankings and totals across all customers are in_scope, even when the answer
  lists customer names next to their revenue or order count. "Top N customers"
  and "best customers" questions are rankings, so they are in_scope.
- Asking about a particular customer (by name or id) is sensitive, whatever is
  asked: their orders, spending, signup date or country.

in_scope examples:
  - "Who are our top 3 customers by revenue?"
  - "Best customers, by total revenue"
  - "Which customers placed the most orders?"
  - "How many customers are in Germany?"
sensitive examples:
  - "What did Bob 3 order?"
  - "Show me David 2's signup date."
  - "How much has Bob 3 spent in total?"
  - "List every order placed by customer 7."

Decide on what the question would reveal under any reasonable reading. A
follow-up about how to measure (e.g. revenue or order count) must not change
your decision, so the same question gets the same decision whether or not it
says how to measure.

Give a short reason. If the question is not in_scope, write message_to_user:
a brief, polite reply that declines and says what the assistant can help
with, and give 2-3 suggestions: example questions this assistant can answer,
close to what the user seemed to want (for a sensitive question, an aggregate
alternative). If it is in_scope, leave message_to_user empty and give no
suggestions."""

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [IN_SCOPE, OUT_OF_SCOPE, SENSITIVE]},
        "reason": {"type": "string"},
        "message_to_user": {"type": "string"},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "reason", "message_to_user", "suggestions"],
    "additionalProperties": False,
}


def check_scope(llm, schema_text, question):
    """Return {"decision", "reason", "message_to_user", "suggestions"}."""
    return llm.complete_json(
        system=f"{INSTRUCTIONS}\n\n{schema_text}",
        user_message=question,
        schema=SCHEMA,
    )
