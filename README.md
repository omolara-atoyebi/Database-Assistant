# Database Assistant

Ask questions about a small SQLite e-commerce dataset in plain language. The
assistant checks the question is in scope, asks a follow-up if it's ambiguous,
writes SQL, runs it through a read-only MCP server (which enforces the
guardrails), repairs failed queries, and answers with the SQL it ran cited.

See `docs/PLAN.md` for the design and build phases.

## Setup

Requires Python 3.10+.

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ui]"
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
```

## Run the UI

```bash
streamlit run db_assistant/ui/app.py
```

Opens a chat page in the browser. The sidebar has example questions (clear,
ambiguous, out of scope, sensitive, and one that triggers a repair) and a
"New conversation" button.

To see the missing-key error, start it with an empty key (overrides `.env`
for this run only):

```bash
ANTHROPIC_API_KEY= streamlit run db_assistant/ui/app.py
```

## Command line

```bash
python -m db_assistant.cli "How many orders were placed in July 2025?"
python -m db_assistant.cli --first-sql "SELECT SUM(revenue) FROM orders" "What is the total revenue?"
```

`--first-sql` replaces the LLM's first query, to force a repair.

## Tests and evaluation

```bash
pytest                                 # offline: no API calls
python tests/eval/run_eval.py          # real API: answer quality + scope consistency
```

## MCP server

The server (`db_assistant/mcp_server/server.py`) exposes `list_tables`,
`describe_table` and `run_query` over stdio, and is registered for Claude Code
in `.mcp.json`. To inspect it by hand:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m db_assistant.mcp_server.server
```

The Inspector web UI (2.10-2.11) drops tool results with `isError: true`; check
rejected queries with its CLI mode instead (`--cli ... --method tools/call`).
