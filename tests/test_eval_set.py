"""Checks the evaluation set itself, offline (no API calls).

- The answer key is right: every reference_sql passes the guardrails and is
  graded as correct against its own expected values.
- The grader catches wrong answers (e.g. the double-counting trap) and scope
  decisions that flip between runs.
"""

import importlib.util
from pathlib import Path

import pytest

from db_assistant.mcp_server import database
from db_assistant.pipeline.orchestrator import ANSWERED, DECLINED, NEEDS_CLARIFICATION, Result
from db_assistant.pipeline.repair import Attempt

# tests/eval is not a package, so load run_eval.py by path.
_spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).parent / "eval" / "run_eval.py"
)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

EVAL_SET = run_eval.load_eval_set()
ANSWER_CASES = EVAL_SET["answer_cases"]
SCOPE_CASES = EVAL_SET["scope_cases"]
CASES_WITH_REFERENCE = [case for case in ANSWER_CASES if "reference_sql" in case]


# --- The eval set is well formed ---------------------------------------------

def test_eval_set_has_enough_cases_with_unique_ids():
    ids = [case["id"] for case in ANSWER_CASES]
    assert len(ids) >= 15
    assert len(ids) == len(set(ids))
    assert len(SCOPE_CASES) >= 10
    assert EVAL_SET["runs_per_case"] >= 2


def test_scope_cases_use_known_decisions():
    for case in SCOPE_CASES:
        assert case["expected"] in {"in_scope", "out_of_scope", "sensitive"}


def test_answer_cases_without_reference_sql_check_something_else():
    for case in ANSWER_CASES:
        if "reference_sql" not in case:
            assert case["expect"], case["id"]


# --- The answer key matches the data ----------------------------------------

@pytest.mark.parametrize("case", CASES_WITH_REFERENCE, ids=lambda case: case["id"])
def test_reference_sql_passes_guardrails_and_is_graded_correct(case):
    result = database.run_query(case["reference_sql"])
    rows = [list(row) for row in result.rows]
    assert run_eval.grade_rows(case["expect"], rows) == []


# --- The grader catches wrong answers ---------------------------------------

def test_double_counted_electronics_revenue_fails():
    case = next(case for case in ANSWER_CASES if case["id"] == "electronics_revenue")
    naive = database.run_query(
        "SELECT ROUND(SUM(o.total_amount), 2) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id WHERE p.category = 'Electronics'"
    )
    assert run_eval.grade_rows(case["expect"], [list(row) for row in naive.rows]) != []


def test_numbers_match_within_tolerance_and_strings_ignore_case():
    assert run_eval.values_match(55409.03, 55409.0300001)
    assert run_eval.values_match(57, 57.0)
    assert not run_eval.values_match(57, 58)
    assert not run_eval.values_match(57, "57")
    assert run_eval.values_match("Board Game", "board game ")


def test_ordered_rows_must_be_in_position():
    expect = {"ordered": True, "rows": [["Headphones"], ["Hoodie"]]}
    assert run_eval.grade_rows(expect, [["Headphones", 1], ["Hoodie", 2]]) == []
    assert run_eval.grade_rows(expect, [["Hoodie", 2], ["Headphones", 1]]) != []
    # Unordered: any position is fine.
    assert run_eval.grade_rows({"rows": expect["rows"]}, [["Hoodie"], ["Headphones"]]) == []


def test_scope_case_that_flips_between_runs_fails():
    case = {"question": "q", "expected": "in_scope"}
    assert run_eval.grade_scope_case(case, ["in_scope"] * 5) == []
    problems = run_eval.grade_scope_case(case, ["in_scope", "sensitive", "in_scope"])
    assert problems and "sensitive x1" in problems[0]


def test_answer_case_checks_status_clarification_repair_and_answer_text():
    case = {
        "expect": {
            "asks_clarification": True,
            "repaired": True,
            "broadened": True,
            "answer_contains": ["10"],
            "value": 5,
        }
    }
    good = Result(
        status=ANSWERED,
        answer="Showing the top 10.",
        rows=[[5]],
        attempts=[Attempt("bad", "Rejected: ..."), Attempt("good", None)],
        scope_note="Broadened.",
    )
    assert run_eval.grade_answer_case(case, good, asked_clarification=True) == []

    bare = Result(status=ANSWERED, answer="Five.", rows=[[5]], attempts=[Attempt("good", None)])
    assert len(run_eval.grade_answer_case(case, bare, asked_clarification=False)) == 4

    declined = Result(status=DECLINED, answer="Sorry.")
    assert run_eval.grade_answer_case({"expect": {"value": 5}}, declined, False)

    unexpected_follow_up = Result(status=NEEDS_CLARIFICATION, answer="Which one?")
    assert run_eval.grade_answer_case({"expect": {"value": 5}}, unexpected_follow_up, True)
