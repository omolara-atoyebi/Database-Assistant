# Database Assistant: Build Plan

Read this at the start of each session. Build one phase at a time, and tick a
phase only after it has been confirmed working.

## Status

- [x] Phase 0: Project setup
- [x] Phase 1: Read-only database layer + guardrails (pytest)
- [x] Phase 2: MCP server
- [ ] Phase 3: Test by hand with MCP Inspector
- [ ] Phase 4: Connect to Claude Code and ask questions in plain language
- [ ] Phase 5: LLM client + schema context + few-shot examples
- [ ] Phase 6: Happy path: question → SQL → answer with cited SQL
- [ ] Phase 7: Scope gate + clarification
- [ ] Phase 8: Self-repair loop with tool use
- [ ] Phase 9: Evaluation set
- [ ] Phase 10: UI

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
- **"Sensitive" means:** questions about an individual customer's personal data, e.g. "What did Bob 3 order?" or "Show me David 2's signup date". Aggregates such as revenue by country or customer counts are fine.
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
- **Files:** `pipeline/repair.py`, `orchestrator.py`, `llm/client.py` (tool-use support), `tests/test_pipeline_repair.py`.
- **Run/test:** `pytest tests/test_pipeline_repair.py -v`. The fake LLM produces bad SQL, then fixed SQL. The tests cover:
  - Validation failure, an execution error and an empty result all trigger a repair.
  - The loop stops after 2 repairs with an honest "couldn't answer".
  - `describe_table` is available as a tool only inside repair.
  - For an empty result, the user is told when the scope was broadened.

  Manual check: `python -m db_assistant.cli "orders from Great Britain"`. It should repair `GB` → `UK`.
- **Done when:** all the tests pass and the manual case repairs itself, with the repair visible in the CLI output.

### Phase 9: Evaluation set
- **Goal:** a repeatable score for answer quality, to catch regressions before the UI.
- **Files:** `tests/eval/questions.yaml` (~15 questions with expected values), `tests/eval/run_eval.py`.
- **Run/test:** `python tests/eval/run_eval.py` (uses the real API; prints pass/fail per question).
- **Done when:** the eval runs end-to-end and you're happy with the pass rate. Failures should be understood and either fixed or noted.

### Phase 10: UI
- **Goal:** a simple Streamlit chat page on top of `orchestrator.py`.
- **Files:** `ui/app.py`, `README.md` (how to run).
- **Run/test:** `streamlit run db_assistant/ui/app.py`.
- **Done when:** in the browser you can:
  - ask a question and see the answer with the executed SQL
  - answer a clarifying question
  - see a decline for an out-of-scope question
  - see when a repair happened

  The UI contains no pipeline logic; it only calls the orchestrator.