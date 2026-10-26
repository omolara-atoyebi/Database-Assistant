"""Runs the evaluation set against the real LLM and prints a score.

    python tests/eval/run_eval.py                 # everything
    python tests/eval/run_eval.py --only scope    # scope consistency only
    python tests/eval/run_eval.py --only answers
    python tests/eval/run_eval.py --runs 2        # fewer runs per scope case
    python tests/eval/run_eval.py --case repair_empty_result --verbose
                                                  # one answer case, full output

Uses the configured LLM (config.py / .env: local Ollama or the Anthropic
API) and starts the real MCP server: about (scope cases x runs) short scope
calls, plus 4-7 calls per answer case. Exits with status 1 if any case fails,
2 if the LLM can't be reached at all.

Every failure is labelled, so the score shows what kind of mistake it was:
  format    the reply wasn't the requested JSON, was cut off, or ignored the tool limit
  judgment  a wrong decision: scope, clarification, broadening the question, no-data note
  sql       the query failed after repairs, returned the wrong rows, or wasn't repaired
  answer    the natural-language answer is missing expected text
  setup     something outside the model failed (server down, ...)

The grading functions are pure and tested offline in tests/test_eval_set.py.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import anyio
import yaml

from db_assistant.llm.client import LLMClient, LLMError, LLMFormatError
from db_assistant.pipeline import mcp_client
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    ERROR,
    FAILED,
    LLM_FORMAT,
    NEEDS_CLARIFICATION,
    answer_question,
    load_schema_text,
)
from db_assistant.pipeline.scope import check_scope

QUESTIONS_FILE = Path(__file__).parent / "questions.yaml"
TOLERANCE = 0.01  # numbers match if within this much

# Failure labels, in the order the score lists them.
FORMAT, JUDGMENT, SQL, ANSWER, SETUP = "format", "judgment", "sql", "answer", "setup"
LABELS = (FORMAT, JUDGMENT, SQL, ANSWER, SETUP)


def load_eval_set(path=QUESTIONS_FILE):
    return yaml.safe_load(path.read_text())


# --- Grading ----------------------------------------------------------------
# grade_rows returns a list of problem strings. grade_answer_case and
# grade_scope_case return (label, problem) pairs. An empty list is a pass.

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


def status_label(result):
    """The label for a result that isn't ANSWERED."""
    if result.status == ERROR:
        return FORMAT if result.error_kind == LLM_FORMAT else SETUP
    if result.status == FAILED:
        return SQL
    return JUDGMENT  # DECLINED or NEEDS_CLARIFICATION when an answer was expected


def grade_answer_case(case, result, asked_clarification):
    expect = case.get("expect", {})
    problems = []
    if expect.get("asks_clarification") and not asked_clarification:
        problems.append((JUDGMENT, "expected a clarifying question first"))
    if result.status != ANSWERED:
        detail = f" ({result.answer})" if result.answer else ""
        if result.status == ERROR and result.error:
            detail += f" [{result.error}]"
        problems.append((status_label(result), f"status was {result.status}, expected answered{detail}"))
        return problems
    problems += [(SQL, problem) for problem in grade_rows(expect, result.rows)]
    if expect.get("repaired") and len(result.attempts) < 2:
        problems.append((SQL, "expected a repair, but the first query was used"))
    if expect.get("broadened") and not result.scope_note:
        problems.append((JUDGMENT, "expected the scope to be broadened, with a note to the user"))
    if expect.get("no_data_note") and not result.data_note:
        problems.append((JUDGMENT, "expected a note that no data matched, with the data's date range"))
    for text in expect.get("answer_contains", []):
        if text.lower() not in result.answer.lower():
            problems.append((ANSWER, f"expected the answer to mention {text!r}"))
    return problems


def grade_scope_case(case, decisions):
    if set(decisions) == {case["expected"]}:
        return []
    counts = ", ".join(f"{decision} x{n}" for decision, n in Counter(decisions).items())
    return [(JUDGMENT, f"expected {case['expected']} every time, got {counts}")]


def error_problem(error):
    """A (label, problem) pair for an exception raised while running a case."""
    return (FORMAT if isinstance(error, LLMFormatError) else SETUP, f"error: {error!r}")


def print_problems(problems):
    for label, problem in problems:
        print(f"        [{label}] {problem}")


def label_counts(problems):
    """'format 1, sql 2' for the labels that occurred, in LABELS order."""
    counts = Counter(label for label, _ in problems)
    return ", ".join(f"{label} {counts[label]}" for label in LABELS if counts[label])


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
    """Return every (label, problem) pair; a case passes if it has none."""
    print(f"Scope decisions ({runs} runs each)")

    async def fetch_schema():
        async with mcp_client.connect() as db:
            return await load_schema_text(db)

    schema_text = anyio.run(fetch_schema)
    passed, all_problems = 0, []
    for case in cases:
        try:
            decisions = [
                check_scope(llm, schema_text, case["question"])["decision"] for _ in range(runs)
            ]
            problems = grade_scope_case(case, decisions)
        except Exception as error:  # report and keep going
            problems = [error_problem(error)]
        passed += not problems
        all_problems += problems
        print(f"  {'PASS' if not problems else 'FAIL'}  {case['question']}")
        print_problems(problems)
    return passed, all_problems


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
    passed, all_problems = 0, []
    for case in cases:
        result = None
        try:
            result, asked = run_answer_case(llm, case)
            problems = grade_answer_case(case, result, asked)
        except Exception as error:  # report and keep going
            problems = [error_problem(error)]
        passed += not problems
        all_problems += problems
        print(f"  {'PASS' if not problems else 'FAIL'}  {case['id']}: {case['question']}")
        print_problems(problems)
        if problems and case.get("trap"):
            print(f"        trap: {case['trap']}")
        if result is not None and (problems or verbose):
            print_details(result)
    return passed, all_problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the evaluation set against the configured LLM.")
    parser.add_argument("--only", choices=["scope", "answers"], help="run only one part")
    parser.add_argument("--runs", type=int, help="runs per scope case (default: from questions.yaml)")
    parser.add_argument("--case", action="append", help="run only this answer case id (repeatable)")
    parser.add_argument("--verbose", action="store_true", help="show details for passing cases too")
    args = parser.parse_args(argv)

    eval_set = load_eval_set()
    try:
        llm = LLMClient()  # checks up front that the LLM is reachable (Ollama: running, model installed)
    except LLMError as error:
        print(f"Can't start the eval: {error}")
        return 2
    print(f"LLM: {llm.model} ({llm.provider})\n")
    score = []
    if args.case:
        args.only = "answers"
        unknown = set(args.case) - {case["id"] for case in eval_set["answer_cases"]}
        if unknown:
            parser.error(f"unknown case id(s): {', '.join(sorted(unknown))}")

    if args.only != "answers":
        cases = eval_set["scope_cases"]
        runs = args.runs or eval_set["runs_per_case"]
        score.append(("scope", *run_scope_cases(llm, cases, runs), len(cases)))
        print()
    if args.only != "scope":
        cases = [case for case in eval_set["answer_cases"] if not args.case or case["id"] in args.case]
        score.append(("answers", *run_answer_cases(llm, cases, args.verbose), len(cases)))
        print()

    parts = []
    for name, passed, problems, total in score:
        labels = label_counts(problems)
        parts.append(f"{name} {passed}/{total}" + (f" (problems: {labels})" if labels else ""))
    print("Score: " + ", ".join(parts))
    return 0 if all(passed == total for _, passed, _, total in score) else 1


if __name__ == "__main__":
    sys.exit(main())
