"""The Streamlit page, run offline with Streamlit's AppTest.

Most tests replace the orchestrator with canned Results: the page only
displays what it gets, so these check that each status is shown correctly
and that session state (follow-ups, buttons, reset) works. The last test runs
the real orchestrator and MCP server with a FakeLLM, inside Streamlit.
"""

import pytest
from streamlit.testing.v1 import AppTest

from db_assistant import config
from db_assistant.llm import client as llm_client
from db_assistant.pipeline import mcp_client, orchestrator
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    DECLINED,
    ERROR,
    FAILED,
    MISSING_API_KEY,
    NEEDS_CLARIFICATION,
    Result,
)
from db_assistant.pipeline.repair import Attempt

APP = str(config.PROJECT_ROOT / "db_assistant" / "ui" / "app.py")

ANSWER = Result(
    status=ANSWERED,
    answer="There were 57 orders in July 2025.",
    sql="SELECT COUNT(*) AS order_count FROM orders",
    columns=["order_count"],
    rows=[[57]],
    attempts=[Attempt("SELECT COUNT(*) AS order_count FROM orders", None)],
    steps=[("scope", None), ("clarify", None), ("generate", None), ("run", 1), ("answer", None)],
)


class FakeOrchestrator:
    """Stands in for orchestrator.answer_question: returns scripted Results, records calls."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, question, clarification=None, on_step=None, **kwargs):
        self.calls.append({"question": question, "clarification": clarification})
        result = self.results.pop(0)
        for step, detail in result.steps:
            on_step(step, detail)
        return result


@pytest.fixture
def app(monkeypatch):
    """Start the page with a scripted orchestrator: app(results) -> (AppTest, fake)."""
    monkeypatch.setattr(orchestrator, "list_allowed_tables", lambda server=None: list(config.ALLOWED_TABLES))

    def start(results=()):
        fake = FakeOrchestrator(results)
        monkeypatch.setattr(orchestrator, "answer_question", fake)
        return AppTest.from_file(APP).run(), fake

    return start


def ask(at, question):
    return at.chat_input[0].set_value(question).run()


def markdown(at):
    return [element.value for element in at.markdown]


def expander(at, label):
    return next(element for element in at.expander if element.label == label)


# --- Sidebar ------------------------------------------------------------------

def test_sidebar_shows_tables_model_examples_and_reset(app):
    at, _ = app()
    sidebar_text = [element.value for element in at.sidebar.markdown]
    assert "`customers`, `products`, `orders`, `order_items`" in sidebar_text
    assert f"`{config.MODEL}`" in sidebar_text
    labels = [button.label for button in at.sidebar.button]
    assert "Who are our best customers?" in labels
    assert "List the orders placed in December 2024" in labels
    assert "New conversation" in labels
    assert not at.exception


def test_tables_are_retried_until_the_server_is_reachable(monkeypatch):
    reachable = []

    def list_allowed_tables(server=None):
        if not reachable:
            raise mcp_client.ServerUnavailable("down")
        return ["customers"]

    monkeypatch.setattr(orchestrator, "list_allowed_tables", list_allowed_tables)
    at = AppTest.from_file(APP).run()
    assert "Can't reach the database server" in at.sidebar.error[0].value
    assert "tables" not in at.session_state  # the failure is not stored

    reachable.append(True)
    at.run()  # any rerun tries again
    assert "`customers`" in [element.value for element in at.sidebar.markdown]
    assert not at.sidebar.error


# --- Answer ---------------------------------------------------------------------

def test_answer_shows_text_table_row_count_and_cited_sql(app):
    at, fake = app([ANSWER])

    ask(at, "How many orders were placed in July 2025?")

    assert fake.calls == [{"question": "How many orders were placed in July 2025?", "clarification": None}]
    assert ANSWER.answer in markdown(at)
    assert at.dataframe[0].value.to_dict("list") == {"order_count": [57]}
    assert "1 row" in [element.value for element in at.caption]
    assert expander(at, "SQL that was run").code[0].value == ANSWER.sql
    assert not any("badge" in value for value in markdown(at))  # no repair
    # The live status listed the steps and ended as "Answered".
    assert at.status[0].label == "Answered"
    assert "Writing SQL" in [element.value for element in at.status[0].markdown]
    assert not at.exception


def test_answer_shows_notes_truncation_and_repair_trace(app):
    repaired = Result(
        status=ANSWERED,
        answer="No orders in December 2024; showing October 2025 instead.",
        sql="SELECT id FROM orders WHERE order_date >= '2025-10-01'",
        columns=["id"],
        rows=[[1], [2]],
        truncated=True,
        attempts=[
            Attempt("SELECT id FROM orders WHERE order_date LIKE '2024-12%'", "The query ran but returned no rows."),
            Attempt(
                "SELECT id FROM orders WHERE order_date >= '2025-10-01'",
                None,
                tool_calls=[{"name": "describe_table", "input": {"table_name": "orders"}, "is_error": False}],
                reason="No data for December 2024; showing the latest month.",
            ),
        ],
        data_note="No data matched the original question. The data covers orders from 2025-04-11 to 2025-10-08.",
        scope_note="Showing the most recent month instead.",
    )
    at, _ = app([repaired])

    ask(at, "List the orders placed in December 2024")

    assert [element.value for element in at.info] == [repaired.data_note, repaired.scope_note]
    assert ":orange-badge[:material/build: Repaired 1 time]" in markdown(at)
    assert "truncated" in at.warning[0].value
    trace = expander(at, "Repair trace")
    assert [element.value for element in trace.code] == [attempt.sql for attempt in repaired.attempts]
    assert trace.error[0].value == "The query ran but returned no rows."
    assert "Called `describe_table` for `orders`" in [element.value for element in trace.caption]


# --- Clarification ---------------------------------------------------------------

def test_follow_up_question_then_typed_reply_continues_the_same_request(app):
    follow_up = Result(
        status=NEEDS_CLARIFICATION,
        answer="Do you mean by total revenue or by number of orders?",
        reason="'Best' could mean either.",
    )
    at, fake = app([follow_up, ANSWER])

    ask(at, "Who are our best customers?")
    assert follow_up.answer in markdown(at)
    assert "Why I'm asking: 'Best' could mean either." in [element.value for element in at.caption]

    ask(at, "by total revenue")

    assert fake.calls[1] == {
        "question": "Who are our best customers?",
        "clarification": (follow_up.answer, "by total revenue"),
    }
    assert at.session_state.pending_clarification is None


def test_example_button_during_a_follow_up_starts_a_new_request(app):
    follow_up = Result(status=NEEDS_CLARIFICATION, answer="Revenue or order count?", reason="Ambiguous.")
    at, fake = app([follow_up, ANSWER])

    ask(at, "Who are our best customers?")
    at.sidebar.button(key="example-Clear question").click().run()

    assert fake.calls[1] == {"question": "How many orders were placed in July 2025?", "clarification": None}


# --- Decline, failure, error -------------------------------------------------------

def test_decline_is_a_warning_with_reason_and_clickable_suggestions(app):
    declined = Result(
        status=DECLINED,
        answer="I can't share one customer's orders.",
        reason="Asks about one named customer.",
        suggestions=["What are the top 3 products by revenue?"],
    )
    at, fake = app([declined, ANSWER])

    ask(at, "What did Bob 3 order?")
    assert at.warning[0].value == declined.answer
    assert "Reason: Asks about one named customer." in [element.value for element in at.caption]
    assert at.status[0].label == "Declined"

    next(button for button in at.button if button.label == declined.suggestions[0]).click().run()
    assert fake.calls[1]["question"] == "What are the top 3 products by revenue?"


def test_failed_after_repairs_is_an_error_with_the_trace(app):
    failed = Result(
        status=FAILED,
        answer="I couldn't answer that: the query still failed after 2 repairs.",
        sql="SELECT nope FROM orders",
        error="Rejected: column 'nope' does not exist.",
        attempts=[Attempt("SELECT a", "Rejected: a"), Attempt("SELECT b", "Rejected: b"), Attempt("SELECT nope FROM orders", "Rejected: nope")],
    )
    at, _ = app([failed])

    ask(at, "What is the total revenue?")

    assert at.error[0].value == failed.answer
    assert ":orange-badge[:material/build: Repaired 2 times]" in markdown(at)
    assert len(expander(at, "Repair trace").code) == 3
    assert at.status[0].label == "Couldn't answer"
    assert not at.exception


def test_missing_api_key_shows_a_friendly_error_through_the_real_orchestrator(monkeypatch):
    # The real answer_question, with no key: no API call, no traceback.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_client, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "list_allowed_tables", lambda server=None: ["customers"])
    at = AppTest.from_file(APP).run()

    ask(at, "How many orders were placed in July 2025?")

    result = at.session_state.messages[-1]["result"]
    assert result.status == ERROR and result.error_kind == MISSING_API_KEY
    assert "no valid Anthropic API key" in at.error[0].value
    assert "ANTHROPIC_API_KEY is not set" in [element.value for element in at.caption][-1]
    assert not at.exception


# --- New conversation --------------------------------------------------------------

def test_new_conversation_resets_everything(app):
    follow_up = Result(status=NEEDS_CLARIFICATION, answer="Revenue or order count?", reason="Ambiguous.")
    at, _ = app([ANSWER, follow_up])
    ask(at, "How many orders were placed in July 2025?")
    ask(at, "Who are our best customers?")
    assert len(at.session_state.messages) == 4
    assert at.session_state.pending_clarification is not None

    at.sidebar.button(key="new-conversation").click().run()

    assert at.session_state.messages == []
    assert at.session_state.pending_clarification is None
    assert not at.chat_message


# --- Real pipeline inside Streamlit -------------------------------------------------

def test_real_orchestrator_and_mcp_server_run_inside_streamlit(fake_llm, monkeypatch):
    # Real answer_question and list_allowed_tables (MCP server subprocess),
    # only the LLM is fake: checks the blocking call works in Streamlit's
    # script thread and live steps reach the page.
    llm = fake_llm([
        {"decision": "in_scope", "reason": "Aggregate.", "message_to_user": "", "suggestions": []},
        {"ambiguous": False, "reason": "Clear.", "clarifying_question": ""},
        "SELECT COUNT(*) AS order_count FROM orders WHERE strftime('%Y-%m', order_date) = '2025-07'",
        "There were 57 orders in July 2025.",
    ])
    monkeypatch.setattr(llm_client, "LLMClient", lambda: llm)
    at = AppTest.from_file(APP, default_timeout=60).run()

    ask(at, "How many orders were placed in July 2025?")

    assert not at.exception
    assert at.dataframe[0].value.to_dict("list") == {"order_count": [57]}
    assert "There were 57 orders in July 2025." in markdown(at)
    assert "`customers`, `products`, `orders`, `order_items`" in [element.value for element in at.sidebar.markdown]
