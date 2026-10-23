"""Runs the evaluation set against the real API and prints a score.

    python tests/eval/run_eval.py                 # everything
    python tests/eval/run_eval.py --only scope    # scope consistency only
    python tests/eval/run_eval.py --only answers
    python tests/eval/run_eval.py --runs 2        # fewer runs per scope case
    python tests/eval/run_eval.py --case repair_empty_result --verbose
                                                  # one answer case, full output

Uses the real LLM and starts the real MCP server, so it costs API calls:
about (scope cases x runs) short scope calls, plus 4-7 calls per answer case.
Exits with status 1 if any case fails.

The grading functions are pure and tested offline in tests/test_eval_set.py.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import anyio
import yaml

from db_assistant.llm.client import LLMClient
from db_assistant.pipeline import mcp_client
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    NEEDS_CLARIFICATION,
    answer_question,
    load_schema_text,
)
from db_assistant.pipeline.scope import check_scope

QUESTIONS_FILE = Path(__file__).parent / "questions.yaml"
TOLERANCE = 0.01  # numbers match if within this much


def load_eval_set(path=QUESTIONS_FILE):
    return yaml.safe_load(path.read_text())


# --- Grading ----------------------------------------------------------------
# Each grade_* function returns a list of problems; an empty list is a pass.

def values_match(expected, actual):
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and abs(actual - expected) <= TOLERANCE
    return str(expected).strip().lower() == str(actual).strip().lower()


def row_contains(row, expected_values):
    """True if every expected value appears somewhere in the row."""
    return all(any(values_match(value, cell) for cell in row) for value in expected_values)


def grade_rows(expect, rows):
    problems = []
    if "value" in expect:
        if not any(values_match(expect["value"], cell) for row in rows for cell in row):
            problems.append(f"expected the value {expect['value']} in the result")
    for position, expected_row in enumerate(expect.get("rows", [])):
        if expect.get("ordered"):
            if position >= len(rows) or not row_contains(rows[position], expected_row):
                problems.append(f"expected row {position + 1} to contain {expected_row}")
        elif not any(row_contains(row, expected_row) for row in rows):
            problems.append(f"expected a row containing {expected_row}")
    return problems


def grade_answer_case(case, result, asked_clarification):
    expect = case.get("expect", {})
    problems = []
    if expect.get("asks_clarification") and not asked_clarification:
        problems.append("expected a clarifying question first")
    if result.status != ANSWERED:
        detail = f" ({result.answer})" if result.answer else ""
        problems.append(f"status was {result.status}, expected answered{detail}")
        return problems
    problems += grade_rows(expect, result.rows)
    if expect.get("repaired") and len(result.attempts) < 2:
        problems.append("expected a repair, but the first query was used")
    if expect.get("broadened") and not result.scope_note:
        problems.append("expected the scope to be broadened, with a note to the user")
    if expect.get("no_data_note") and not result.data_note:
        problems.append("expected a note that no data matched, with the data's date range")
    for text in expect.get("answer_contains", []):
        if text.lower() not in result.answer.lower():
            problems.append(f"expected the answer to mention {text!r}")
    return problems


def grade_scope_case(case, decisions):
    if set(decisions) == {case["expected"]}:
        return []
    counts = ", ".join(f"{decision} x{n}" for decision, n in Counter(decisions).items())
    return [f"expected {case['expected']} every time, got {counts}"]


# --- Running ----------------------------------------------------------------

def run_answer_case(llm, case):
    """Return (result, asked_clarification). Answers one follow-up if the case has one."""
    result = answer_question(case["question"], llm=llm, first_sql=case.get("first_sql"))
    asked = result.status == NEEDS_CLARIFICATION
    if asked and "clarification_answer" in case:
        result = answer_question(
            case["question"],
            llm=llm,
            clarification=(result.answer, case["clarification_answer"]),
            first_sql=case.get("first_sql"),
        )
    return result, asked


def run_scope_cases(llm, cases, runs):
    print(f"Scope decisions ({runs} runs each)")

    async def fetch_schema():
        async with mcp_client.connect() as db:
            return await load_schema_text(db)

    schema_text = anyio.run(fetch_schema)
    passed = 0
    for case in cases:
        try:
            decisions = [
                check_scope(llm, schema_text, case["question"])["decision"] for _ in range(runs)
            ]
            problems = grade_scope_case(case, decisions)
        except Exception as error:  # report and keep going
            problems = [f"error: {error!r}"]
        passed += not problems
        print(f"  {'PASS' if not problems else 'FAIL'}  {case['question']}")
        for problem in problems:
            print(f"        {problem}")
    return passed


def print_details(result):
    print(f"        status: {result.status}")
    for number, attempt in enumerate(result.attempts, start=1):
        print(f"        attempt {number}: " + attempt.sql.replace("\n", "\n                   "))
        for call in attempt.tool_calls:
            print(f"          called {call['name']}({call['input']})")
        print(f"          -> {attempt.problem or 'OK'}")
    if result.data_note:
        print(f"        data_note: {result.data_note}")
    if result.scope_note:
        print(f"        scope_note: {result.scope_note}")
    print(f"        rows: {result.rows[:5]}{' ...' if len(result.rows) > 5 else ''}")
    print(f"        answer: {result.answer}")


def run_answer_cases(llm, cases, verbose=False):
    print("Answers")
    passed = 0
    for case in cases:
        result = None
        try:
            result, asked = run_answer_case(llm, case)
            problems = grade_answer_case(case, result, asked)
        except Exception as error:  # report and keep going
            problems = [f"error: {error!r}"]
        passed += not problems
        print(f"  {'PASS' if not problems else 'FAIL'}  {case['id']}: {case['question']}")
        for problem in problems:
            print(f"        {problem}")
        if problems and case.get("trap"):
            print(f"        trap: {case['trap']}")
        if result is not None and (problems or verbose):
            print_details(result)
    return passed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the evaluation set against the real API.")
    parser.add_argument("--only", choices=["scope", "answers"], help="run only one part")
    parser.add_argument("--runs", type=int, help="runs per scope case (default: from questions.yaml)")
    parser.add_argument("--case", action="append", help="run only this answer case id (repeatable)")
    parser.add_argument("--verbose", action="store_true", help="show details for passing cases too")
    args = parser.parse_args(argv)

    eval_set = load_eval_set()
    llm = LLMClient()
    score = []
    if args.case:
        args.only = "answers"
        unknown = set(args.case) - {case["id"] for case in eval_set["answer_cases"]}
        if unknown:
            parser.error(f"unknown case id(s): {', '.join(sorted(unknown))}")

    if args.only != "answers":
        cases = eval_set["scope_cases"]
        runs = args.runs or eval_set["runs_per_case"]
        score.append(("scope", run_scope_cases(llm, cases, runs), len(cases)))
        print()
    if args.only != "scope":
        cases = [case for case in eval_set["answer_cases"] if not args.case or case["id"] in args.case]
        score.append(("answers", run_answer_cases(llm, cases, args.verbose), len(cases)))
        print()

    print("Score: " + ", ".join(f"{name} {passed}/{total}" for name, passed, total in score))
    return 0 if all(passed == total for _, passed, total in score) else 1


if __name__ == "__main__":
    sys.exit(main())
