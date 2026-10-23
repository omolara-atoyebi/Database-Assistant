# Database Assistant: Build Plan

Read this at the start of each session. Build one phase at a time, and tick a
phase only after it has been confirmed working.

## Status

- [x] Phase 0: Project setup
- [x] Phase 1: Read-only database layer + guardrails (pytest)
- [x] Phase 2: MCP server
- [x] Phase 3: Test by hand with MCP Inspector
- [x] Phase 4: Connect to Claude Code and ask questions in plain language
- [x] Phase 5: LLM client + schema context + few-shot examples
- [x] Phase 6: Happy path: question → SQL → answer with cited SQL
- [x] Phase 7: Scope gate + clarification
- [x] Phase 8: Self-repair loop with tool use
- [x] Phase 9: Evaluation set
- [x] Phase 10a: Orchestrator support for the UI (steps, suggestions, friendly errors)
- [ ] Phase 10b: Streamlit UI

## Context

The product is a natural-language-to-SQL assistant (UI → pipeline → Claude API +
MCP server → SQLite), described in `CLAUDE.md` and `docs/process_flow.png`. The
build order starts with the MCP server and its safety checks, tests it by hand,
and only then builds the pipeline. The UI comes last.

---

## Folder and file structure

```
Database-Assistant/
├── CLAUDE.md                  Instructions for Claude Code (dev tool, not product)
├── README.md                  What the project is and how to run it
├── .gitignore                 Ignores .env, .venv/, .DS_Store, __pycache__/, caches
├── .env.example               Template for .env (ANTHROPIC_API_KEY=...), safe to commit
├── .mcp.json                  Registers our MCP server with Claude Code for manual testing
├── pyproject.toml             Dependencies (anthropic, mcp, streamlit, pytest) and pytest config
├── data/
│   └── analytics.db           The read-only SQLite dataset
├── docs/
│   ├── process_flow.png       Design diagram
│   └── PLAN.md                This build plan
├── db_assistant/              The application package
│   ├── __init__.py
│   ├── config.py              One place for settings: DB path, allowed tables, row limit, timeout, model name
│   ├── mcp_server/            The MCP server (phases 1-4)
│   │   ├── __init__.py
│   │   ├── guardrails.py      Safety checks: SELECT-only, allowed tables/columns
│   │   ├── database.py        Opens the DB read-only; runs a query with row limit + timeout
│   │   └── server.py          MCP entry point: tools list_tables, describe_table, run_query
│   ├── llm/
│   │   ├── __init__.py
│   │   └── client.py          The only module that talks to the Anthropic API (swap point)
│   ├── pipeline/              The workflow (phases 5-8)
│   │   ├── __init__.py
│   │   ├── schema_context.py  Builds the schema text: tables, keys, types, value lists
│   │   ├── few_shot.py        The 5 question→SQL examples covering the dataset's traps
│   │   ├── scope.py           Scope gate: in_scope / out_of_scope / sensitive + reason
│   │   ├── clarify.py         Decides if a question is ambiguous; writes the follow-up question
│   │   ├── generate.py        Asks the LLM for SQL and extracts a clean query
│   │   ├── repair.py          Self-repair loop (max 2), with describe_table tool use
│   │   ├── mcp_client.py      Connects the pipeline to our MCP server
│   │   ├── answer.py          Turns rows into a natural-language answer + cited SQL
│   │   └── orchestrator.py    Runs the steps in order; the single entry point for the UI
│   ├── cli.py                 `python -m db_assistant.cli "question"`: test without a UI
│   └── ui/
│       └── app.py             Streamlit UI (last phase)
└── tests/
    ├── conftest.py            Shared fixtures, including a fake LLM for offline tests
    ├── test_guardrails.py     Unsafe SQL rejected, safe SQL allowed
    ├── test_database.py       Read-only access, row limit, timeout
    ├── test_mcp_tools.py      The three MCP tools return the right shapes and errors
    ├── test_pipeline_*.py     Each pipeline step tested with the fake LLM (no API calls)
    └── eval/
        ├── questions.yaml     Real questions with expected answers
        └── run_eval.py        Runs them end-to-end against the real API, prints a score
```

### Guardrail design

Use what Python's built-in `sqlite3` already offers instead of writing an SQL
parser:
- Open the file read-only (`file:data/analytics.db?mode=ro`), so writes fail at
  the database level.
- Use `set_authorizer` to allow only reads, and only from allowed tables and
  columns. SQLite calls this for every table/column a query touches, so it
  covers column validation for free.
- Use `set_progress_handler` to stop queries that run past the timeout.
- Use `fetchmany(limit + 1)` to enforce the row limit and report truncation.
- Also do a quick check up front so error messages are clear: one statement
  only, must start with `SELECT` or `WITH`.

**Only `run_query` accepts SQL.** `list_tables` and `describe_table` are built
in server code from the allowed-table list in `config.py`. `list_tables`
returns that list. `describe_table` first checks the table name is on the list,
and only then reads its columns using fixed SQL written in the server. Neither
tool ever runs SQL supplied by a user or the LLM.

The tests in `tests/` all run offline. Only `tests/eval/` and the manual steps
call the real API.

---

## Phases

### Phase 0: Project setup
- **Goal:** a working Python environment where `pytest` runs.
- **Files:** `pyproject.toml`, `.gitignore`, `.env.example`, `db_assistant/__init__.py`,
  `db_assistant/config.py`, `tests/conftest.py`, a single smoke test; update `CLAUDE.md` commands.
- **Run/test:** `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`, then `pytest`.
- **Done when:** `pytest` passes; `git status` doesn't show `.env`, `.venv/` or `.DS_Store`.

### Phase 1: Read-only database layer + guardrails (pytest)
- **Goal:** a function that runs one SQL query safely, or rejects it with a clear reason.
- **Files:** `mcp_server/guardrails.py`, `mcp_server/database.py`, `tests/test_guardrails.py`, `tests/test_database.py`.
- **Run/test:** `pytest tests/test_guardrails.py tests/test_database.py -v`; run a single test with `pytest tests/test_guardrails.py::test_drop_rejected`.
- **Done when:** these tests pass:
  - Rejected: `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ATTACH`, `PRAGMA`, two statements in one, an unknown table, an unknown column, `sqlite_master`.
  - Allowed: a valid `SELECT`/`WITH` query, which returns rows.
  - More than the row limit returns only the limit and says the result was cut off.
  - A runaway query (e.g. an infinite recursive CTE) stops at the timeout.

### Phase 2: MCP server
- **Goal:** expose `list_tables`, `describe_table` and `run_query` as MCP tools built on Phase 1.
- **Files:** `mcp_server/server.py`, `tests/test_mcp_tools.py`.
- **Run/test:** `pytest tests/test_mcp_tools.py -v`; start the server with `python -m db_assistant.mcp_server.server`. It waits silently for input over stdio; stop it with Ctrl-C.
- **Done when:** tests show:
  - Each tool returns the right result.
  - `run_query` returns a guardrail error (not a crash) for unsafe SQL.
  - `list_tables` returns exactly the allowed-table list.
  - `describe_table` rejects names not on the list (e.g. `sqlite_master`, `orders; DROP TABLE orders`) without running any query.

### Phase 3: Test by hand with MCP Inspector
- **Goal:** see and use the server's tools in a browser, the way an MCP client would.
- **Files:** none, except notes in `README.md`.
- **Run/test:** `npx @modelcontextprotocol/inspector .venv/bin/python -m db_assistant.mcp_server.server` (needs Node.js). In the browser: Connect, then Tools, then call each tool.
- **Known Inspector bug:** the web UI (2.10.1 and 2.11.0) drops every tool result with `isError: true` and times out after 60s. The server replies correctly within milliseconds (confirmed with a stdio wiretap and `test_rejected_queries_return_promptly_over_stdio`). Check the error cases with Inspector's CLI mode instead:
  ```bash
  npx @modelcontextprotocol/inspector --cli .venv/bin/python -m db_assistant.mcp_server.server --method tools/call --tool-name run_query --tool-arg "sql=DELETE FROM orders"
  npx @modelcontextprotocol/inspector --cli .venv/bin/python -m db_assistant.mcp_server.server --method tools/call --tool-name run_query --tool-arg "sql=SELECT * FROM sqlite_master"
  ```
- **Done when:** you have checked that (this is the real proof of the server's guardrails, because you send the SQL directly with no LLM in between):
  - All three tools are listed with descriptions (web UI).
  - `describe_table orders` shows the columns (web UI).
  - A `SELECT` returns rows (web UI).
  - `DELETE FROM orders` and `SELECT * FROM sqlite_master` are both rejected with a readable message (CLI: `"isError": true` and a `Rejected:` message listing the allowed tables).

### Phase 4: Connect to Claude Code and ask questions in plain language
- **Goal:** prove the server is useful to an LLM before writing any pipeline code. Claude Code acts as a stand-in client here; it is not part of the product.
- **Files:** `.mcp.json` (project-scoped server registration).
- **Run/test:** restart Claude Code in the repo, approve the server, and check `/mcp` shows it connected. Then ask, for example:
  - "Which country has the most revenue?"
  - "Monthly order count in 2025"
  - "Top 3 products by units sold"
  - "Delete all orders" (must not succeed)
- **Done when:**
  - The answers match what you get by running `sqlite3` directly.
  - The delete attempt fails, whether Claude Code refuses or the server rejects it. Phase 3 already proved the server guardrails.

  Note any confusing tool descriptions or error messages and fix them before Phase 5.

### Phase 5: LLM client + schema context + few-shot examples
- **Goal:** a single swappable module for LLM calls, and the prompt context the LLM will see.
- **Files:** `llm/client.py`, `pipeline/schema_context.py`, `pipeline/few_shot.py`, `tests/test_pipeline_context.py`, `tests/conftest.py` (fake LLM).
- **Run/test:** `pytest tests/test_pipeline_context.py -v`; `python -m db_assistant.llm.client` sends one "hello" call to the API to check your key works.
- **Done when:**
  - The schema text includes all 4 tables, foreign keys, the country and category value lists, and the 5 examples:
    1. Bucketing text dates by month
    2. Avoiding double-counting `total_amount`
    3. Revenue from `unit_price`, not `products.price`
    4. `UK`, not `GB`
    5. Revenue by category per country (a join across all four tables)
  - Every example's SQL passes Phase 1's guardrails and runs successfully.
  - The live call works.

### Phase 6: Happy path: question → SQL → answer with cited SQL
- **Goal:** answer a clear, in-scope question end-to-end from the command line.
- **Files:** `pipeline/generate.py`, `pipeline/mcp_client.py`, `pipeline/answer.py`, `pipeline/orchestrator.py`, `cli.py`, `tests/test_pipeline_generate.py`.
- **Run/test:** `pytest tests/test_pipeline_generate.py -v` (fake LLM: extracts SQL from fenced or unfenced replies); then `python -m db_assistant.cli "How many orders were placed in July 2025?"`.
- **Done when:** the CLI prints a correct answer plus the SQL that was run, with SQL executed through the MCP server (not directly against SQLite).

### Phase 7: Scope gate + clarification
- **Goal:** the LLM declines out-of-scope or sensitive questions, and asks a follow-up when a question is ambiguous.
- **"Sensitive" means:** questions that target one specific customer (by name or id), e.g. "What did Bob 3 order?", "Show me David 2's signup date" or "How much has Bob 3 spent?". Aggregates such as revenue by country or customer counts are fine, and so are rankings across all customers even when they show names, e.g. "Top 3 customers by revenue". The scope gate decides before clarification, and answering a follow-up must not turn an in-scope question into a decline.
- **Default limit:** a "top"/"best" question with no number uses `config.DEFAULT_TOP_N` (10), and the answer says so.
- **Files:** `pipeline/scope.py`, `pipeline/clarify.py`, `orchestrator.py` (wire in), `cli.py` (ask the follow-up interactively), `tests/test_pipeline_scope.py`.
- **Run/test:** `pytest tests/test_pipeline_scope.py -v` (checks routing with fake LLM decisions). Then try the CLI with:
  - "What's the weather in Berlin?" (should decline: out of scope)
  - "What did Bob 3 order?" (should decline: sensitive)
  - "How many customers are in Germany?" (should answer: an aggregate, not sensitive)
  - "Who are our best customers?" (should ask revenue or order count)
  - "Revenue by category" (should answer directly)
- **Done when:** all five CLI cases behave as described, and the decisions come from the LLM (structured output with a reason), not keyword rules.

### Phase 8: Self-repair loop with tool use
- **Goal:** recover from bad SQL or empty results, with at most 2 repairs.
- **Files:** `pipeline/repair.py`, `orchestrator.py`, `llm/client.py` (tool-use support), `cli.py` (`--first-sql` debug flag), `tests/test_pipeline_repair.py`.
- **Run/test:** `pytest tests/test_pipeline_repair.py -v`. The fake LLM produces bad SQL, then fixed SQL. The tests cover:
  - Validation failure, an execution error and an empty result all trigger a repair.
  - The loop stops after 2 repairs with an honest "couldn't answer".
  - `describe_table` is available as a tool only inside repair.
  - For an empty result, the user is told when the scope was broadened.

  Manual checks with the real LLM. (Not "Great Britain": few-shot example 4 already teaches `UK`, so that question is answered correctly first time and never needs a repair.)
  1. **Empty result:** `python -m db_assistant.cli "List the orders placed in December 2024"`. The data starts on 2025-04-11, so correct SQL returns no rows. This triggers a repair: the LLM checks types and date format first, then broadens the scope, and tells you the data covers 2025-04-11 to 2025-10-08.
  2. **Validation failure:** `python -m db_assistant.cli --first-sql "SELECT SUM(revenue) FROM orders" "What is the total revenue?"`. The debug flag replaces the LLM's first attempt with SQL that uses a column that doesn't exist. The server rejects it, and the real LLM has to repair it. It can call `describe_table` to see the real columns.
- **Done when:** all the tests pass, and both manual cases end in a correct answer. The CLI output shows the failed attempt, the repair (including any `describe_table` call), and, for case 1, the note that the scope was broadened.

### Phase 9: Evaluation set
- **Goal:** a repeatable score for answer quality, to catch regressions before the UI.
- **Files:** `tests/eval/questions.yaml` (~15 questions with expected values), `tests/eval/run_eval.py`.
- **Scope consistency:** `questions.yaml` already has `scope_cases` from the Phase 7 sensitivity fix. The runner must run each one `runs_per_case` times and fail any case whose decision differs between runs.
- **Run/test:** `python tests/eval/run_eval.py` (uses the real API; prints pass/fail per question).
- **Done when:** the eval runs end-to-end and you're happy with the pass rate. Failures should be understood and either fixed or noted.

### Phase 10: UI

**Rule:** the UI contains no pipeline logic. It only calls the orchestrator
and displays what comes back. If the result doesn't carry something the UI
needs, change the result object, not the UI.

**MCP connection:** one per request. Each `answer_question(...)` call starts
the MCP server subprocess over stdio, loads the schema through it, runs the
steps and closes it (~1s, small next to the LLM calls). A clarification reply
is a new request. No long-lived connection across Streamlit reruns.

**Decisions:** no structured SQL reply (`generate.py` and the Phase 6-8 tests
stay as they are); there is no separate default-limit field, since the answer
text and the cited SQL already show the limit.

### Phase 10a: Orchestrator support for the UI
- **Goal:** everything the UI needs is in the orchestrator's result, tested offline.
- **Changes:**
  - `on_step(step, detail)` callback on `answer_question`, called as each step starts: `scope`, `clarify`, `generate`, `run` (attempt n), `repair` (n), `describe_table` (table), `answer`. The same events are kept in `Result.steps`. `describe_table` events are reported after the repair call returns, on the caller's thread (Streamlit can only update the page from its own thread).
  - Scope gate returns `suggestions` (2-3 questions it can answer) for a decline -> `Result.suggestions`.
  - New status `error` with `Result.error_kind`: `missing_api_key`, `mcp_unavailable`, `llm_error`, and a friendly `answer` (no tracebacks). `failed` stays "couldn't answer after 2 repairs".
    - `llm/client.py` raises its own `LLMNotConfigured` (no key after loading `.env`, or the API rejects the key) and `LLMUnavailable` (connection or API errors), so Anthropic exceptions stay in that module.
    - `mcp_client.py` raises `ServerUnavailable` if the server can't start or the connection breaks.
  - `Result.repair_count` (number of repairs) for the "Repaired N time(s)" badge.
  - `orchestrator.list_allowed_tables()` for the sidebar, via the MCP server's `list_tables`.
  - Empty periods: the data's date range (MIN/MAX `order_date`, fetched through MCP) is added to the schema context. If any attempt returns no rows, `Result.data_note` says no data matched and gives the date range; it is computed in code, not by the LLM, and the answer step is told to start with it. If a repair broadens the question, `Result.scope_note` is always set (a fallback text if the LLM left its note empty).
  - `cli.py` shows the `error` status and the notes.
- **Files:** `pipeline/orchestrator.py`, `pipeline/scope.py`, `pipeline/mcp_client.py`, `pipeline/schema_context.py`, `pipeline/answer.py`, `llm/client.py`, `cli.py`, `tests/test_pipeline_ui_contract.py`.
- **Run/test:** `pytest -v`; `python -m db_assistant.cli "List the orders placed in December 2024"` shows the no-data note with the date range.
- **Done when:** all tests pass, including: step order; error statuses for a missing key, an unreachable MCP server and an LLM failure; suggestions on a decline; `repair_count`; `list_allowed_tables`; an empty period yields a data note with the date range that reaches the answer step; a broadened repair always sets `scope_note`.

### Phase 10b: Streamlit UI
- **Goal:** a simple Streamlit chat page on top of `orchestrator.py`.
- **Files:** `ui/__init__.py`, `ui/app.py`, `README.md` (how to run). Install with `pip install -e ".[dev,ui]"`.
- **Layout:**
  - Chat: history above, `st.chat_input` at the bottom.
  - Sidebar: what this is; allowed tables (`list_allowed_tables()`); model name (`config.MODEL`); example buttons (clear: "How many orders were placed in July 2025?", ambiguous: "Who are our best customers?", out of scope: "What's the weather in Berlin?", sensitive: "What did Bob 3 order?", repair: "List the orders placed in December 2024"); "New conversation" clears `session_state`.
- **Per status:**
  - `answered`: answer; data note / scope note if set (always shown); table with row count; truncation note; repair badge + trace expander if repaired; "SQL that was run" expander.
  - `needs_clarification`: the question as a chat message, "why I'm asking" in small text. The pending request is kept in `session_state`; the next message is sent as `clarification=...`.
  - `declined`: warning style, with the reason and the suggestions.
  - `failed` / `error`: error style with the friendly message; `failed` also shows the repair trace.
- **While working:** `st.status` lists the steps as `on_step` reports them.
- **Repair trace:** per attempt the SQL, the error it got, any `describe_table` call, the LLM's reason, and the revised SQL.
- **Out of scope:** login, deployment, saving history between sessions, charts.
- **Run/test:** `streamlit run db_assistant/ui/app.py`.
- **Done when** (checked in the browser):
  - a clear question gets an answer, table and cited SQL
  - "Who are our best customers?" asks a follow-up and then answers
  - an out-of-scope question is declined
  - a repair case shows the repair trace
  - missing API key shows a friendly error
  - the example buttons work and "New conversation" resets everything