"""Ask the assistant one question from the command line.

    python -m db_assistant.cli "How many orders were placed in July 2025?"

If the question is ambiguous, the assistant asks a follow-up; type your answer.

Force a repair by replacing the LLM's first SQL:
    python -m db_assistant.cli --first-sql "SELECT SUM(revenue) FROM orders" "What is the total revenue?"
"""

import argparse
import sys

from db_assistant.llm.client import LLMClient
from db_assistant.pipeline.orchestrator import (
    DECLINED,
    FAILED,
    NEEDS_CLARIFICATION,
    answer_question,
)


def print_attempts(attempts):
    """Show the failed attempts and repairs, if there were any."""
    if len(attempts) < 2 and not attempts[0].problem:
        return
    for number, attempt in enumerate(attempts, start=1):
        label = "First attempt" if number == 1 else f"Repair {number - 1}"
        print(f"--- {label} ---")
        for call in attempt.tool_calls:
            failed = " (failed)" if call["is_error"] else ""
            print(f"  called {call['name']}({call['input']}){failed}")
        if attempt.reason:
            print(f"  why: {attempt.reason}")
        print("  " + attempt.sql.replace("\n", "\n  "))
        print(f"  -> {attempt.problem or 'OK'}")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ask the database assistant a question.")
    parser.add_argument("question", help="the question, in plain language")
    parser.add_argument("--first-sql", help="debug: use this SQL instead of the LLM's first attempt")
    args = parser.parse_args(argv)

    llm = LLMClient()
    result = answer_question(args.question, llm=llm, first_sql=args.first_sql)

    if result.status == NEEDS_CLARIFICATION:
        print(f"Assistant: {result.answer}")
        print(f"  (why: {result.reason})")
        user_answer = input("You: ")
        result = answer_question(
            args.question,
            llm=llm,
            clarification=(result.answer, user_answer),
            first_sql=args.first_sql,
        )

    if result.status == DECLINED:
        print(result.answer)
        print(f"  (declined: {result.reason})")
        return 0

    print_attempts(result.attempts)
    if result.scope_note:
        print(f"Note: the question was broadened. {result.scope_note}\n")
    print(result.answer)
    if result.sql:
        print("\nSQL that was run:")
        print(result.sql)
    if result.status == FAILED:
        print(f"\nServer error: {result.error}")
        return 1
    print(f"\n({len(result.rows)} row(s){', truncated' if result.truncated else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
