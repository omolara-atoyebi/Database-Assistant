"""Streamlit chat UI for the Database Assistant.

    streamlit run db_assistant/ui/app.py

Display only: every answer comes from orchestrator.answer_question, and
everything shown is read from the Result it returns. No pipeline logic here
(docs/PLAN.md, Phase 10).

Session state:
  messages               the history: {"role": "user", "text"} or {"role": "assistant", "result"}
  pending_clarification  (original question, follow-up question) while a follow-up is open
  queued_question        set by an example or suggestion button; handled like typed input
  tables                 the allowed tables, stored only once list_tables succeeded
"""

import pandas as pd
import streamlit as st

from db_assistant import config
from db_assistant.pipeline import orchestrator
from db_assistant.pipeline.mcp_client import ServerUnavailable
from db_assistant.pipeline.orchestrator import (
    ANSWERED,
    DECLINED,
    ERROR,
    FAILED,
    NEEDS_CLARIFICATION,
    STEP_ANSWER,
    STEP_CLARIFY,
    STEP_DESCRIBE_TABLE,
    STEP_GENERATE,
    STEP_REPAIR,
    STEP_RUN,
    STEP_SCOPE,
)

EXAMPLES = [
    ("Clear question", "How many orders were placed in July 2025?"),
    ("Ambiguous: asks a follow-up", "Who are our best customers?"),
    ("Out of scope: declined", "What's the weather in Berlin?"),
    ("Sensitive: declined", "What did Bob 3 order?"),
    ("No data: triggers a repair", "List the orders placed in December 2024"),
]

STEP_LABELS = {
    STEP_SCOPE: "Checking scope",
    STEP_CLARIFY: "Checking if the question is ambiguous",
    STEP_GENERATE: "Writing SQL",
    STEP_RUN: "Validating and running the query (attempt {})",
    STEP_REPAIR: "Repairing the query (repair {})",
    STEP_DESCRIBE_TABLE: "Looked up the columns of table {}",
    STEP_ANSWER: "Writing the answer",
}

STATUS_LABELS = {
    ANSWERED: "Answered",
    NEEDS_CLARIFICATION: "Needs a follow-up",
    DECLINED: "Declined",
    FAILED: "Couldn't answer",
    ERROR: "Something went wrong",
}


def step_label(step, detail):
    label = STEP_LABELS.get(step, step)
    return label.format(detail) if "{}" in label else label


def text(value):
    """Markdown-safe text: Streamlit reads $...$ as LaTeX, and answers contain prices."""
    return value.replace("$", "\\$")


# --- State and callbacks -------------------------------------------------------

def init_state():
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending_clarification", None)
    st.session_state.setdefault("queued_question", None)


def queue_question(question):
    # A button always starts a new request, so it drops an open follow-up.
    st.session_state.queued_question = question
    st.session_state.pending_clarification = None


def new_conversation():
    st.session_state.clear()


# --- Rendering a Result ----------------------------------------------------------

def render_steps(result):
    if result.steps:
        with st.expander("Steps"):
            for step, detail in result.steps:
                st.text(step_label(step, detail))


def render_repair_trace(result):
    if not result.repair_count:
        return
    with st.expander("Repair trace"):
        for number, attempt in enumerate(result.attempts, start=1):
            st.markdown(f"**{'First attempt' if number == 1 else f'Repair {number - 1}'}**")
            for call in attempt.tool_calls:
                failed = " (failed)" if call["is_error"] else ""
                st.caption(f"Called `{call['name']}` for `{call['input'].get('table_name')}`{failed}")
            if attempt.reason:
                st.caption(f"Why: {text(attempt.reason)}")
            st.code(attempt.sql, language="sql")
            if attempt.problem:
                st.error(attempt.problem)
            else:
                st.success("This query worked.")


def render_repair_badge(result):
    if result.repair_count:
        times = "time" if result.repair_count == 1 else "times"
        st.badge(f"Repaired {result.repair_count} {times}", icon=":material/build:", color="orange")


def render_answered(result):
    render_repair_badge(result)
    st.markdown(text(result.answer))
    for note in (result.data_note, result.scope_note):
        if note:
            st.info(text(note), icon=":material/info:")
    st.dataframe(pd.DataFrame(result.rows, columns=result.columns), hide_index=True)
    rows = len(result.rows)
    st.caption(f"{rows} row{'' if rows == 1 else 's'}")
    if result.truncated:
        st.warning(f"The result was truncated to the first {rows} rows.")
    render_repair_trace(result)
    with st.expander("SQL that was run"):
        st.code(result.sql, language="sql")


def render_clarification(result):
    st.markdown(text(result.answer))
    st.caption(f"Why I'm asking: {text(result.reason)}")
    st.caption("Type your answer below.")


def render_declined(result, key):
    st.warning(text(result.answer), icon=":material/block:")
    st.caption(f"Reason: {text(result.reason)}")
    if result.suggestions:
        st.markdown("You could ask instead:")
        for number, suggestion in enumerate(result.suggestions):
            st.button(suggestion, key=f"suggestion-{key}-{number}", on_click=queue_question, args=(suggestion,))


def render_failed(result):
    render_repair_badge(result)
    st.error(text(result.answer), icon=":material/error:")
    if result.error:
        st.caption(f"Last error: {result.error}")
    render_repair_trace(result)


def render_error(result):
    st.error(text(result.answer), icon=":material/error:")
    if result.error:
        st.caption(result.error)


def render_result(result, key):
    if result.status == ANSWERED:
        render_answered(result)
    elif result.status == NEEDS_CLARIFICATION:
        render_clarification(result)
    elif result.status == DECLINED:
        render_declined(result, key)
    elif result.status == FAILED:
        render_failed(result)
    else:
        render_error(result)
    render_steps(result)


# --- Running a request -------------------------------------------------------

def run_request(question, pending):
    """Call the orchestrator, showing each step live. Returns the Result."""
    with st.status("Working…", expanded=True) as status:

        def on_step(step, detail):
            status.write(step_label(step, detail))

        if pending:
            original, follow_up = pending
            result = orchestrator.answer_question(
                original, clarification=(follow_up, question), on_step=on_step
            )
        else:
            result = orchestrator.answer_question(question, on_step=on_step)

        failed = result.status in (FAILED, ERROR)
        status.update(label=STATUS_LABELS[result.status], state="error" if failed else "complete", expanded=False)
    return result


# --- Page --------------------------------------------------------------------

def render_sidebar():
    with st.sidebar:
        st.header("Database Assistant")
        st.write(
            "Ask questions about a small e-commerce dataset (customers, products, "
            "orders) in plain language. The assistant writes SQL, runs it through a "
            "read-only database server, and answers with the SQL it ran."
        )

        st.subheader("Allowed tables")
        if "tables" not in st.session_state:
            try:
                st.session_state.tables = orchestrator.list_allowed_tables()
            except ServerUnavailable:
                pass  # not stored, so the next rerun tries again
        if "tables" in st.session_state:
            st.markdown(", ".join(f"`{table}`" for table in st.session_state.tables))
        else:
            st.error("Can't reach the database server. Retrying on your next action.")

        st.subheader("Model")
        where = "local, via Ollama" if config.LLM_PROVIDER == config.OLLAMA else "via the Anthropic API"
        st.markdown(f"`{config.MODEL}` ({where})")

        st.subheader("Examples")
        for label, question in EXAMPLES:
            st.button(question, key=f"example-{label}", help=label, on_click=queue_question, args=(question,), width="stretch")

        st.divider()
        st.button("New conversation", key="new-conversation", on_click=new_conversation, type="primary", width="stretch")


def main():
    st.set_page_config(page_title="Database Assistant", page_icon=":material/database:")
    init_state()
    render_sidebar()

    st.title("Database Assistant")
    for number, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(text(message["text"]))
            else:
                render_result(message["result"], key=number)

    pending = st.session_state.pending_clarification
    # Keep the placeholder constant: changing a widget's arguments makes
    # Streamlit treat it as a new widget, and the typed reply would be lost.
    typed = st.chat_input("Ask a question about the data…", key="chat-input")
    queued = st.session_state.queued_question
    st.session_state.queued_question = None
    question = queued or typed
    if not question:
        return

    # Typed text answers an open follow-up; a queued button question never does.
    pending = pending if not queued else None
    st.session_state.pending_clarification = None

    st.session_state.messages.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(text(question))
    with st.chat_message("assistant"):
        result = run_request(question, pending)
        render_result(result, key=len(st.session_state.messages))
    st.session_state.messages.append({"role": "assistant", "result": result})

    if result.status == NEEDS_CLARIFICATION:
        st.session_state.pending_clarification = (question, result.answer)


main()
