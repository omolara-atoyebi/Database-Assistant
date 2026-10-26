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
cp .env.example .env        # choose the LLM provider
```

### Runtime LLM: local Ollama (default) or Claude

The runtime LLM is chosen in `.env` (see `.env.example`), with no code change:

- `LLM_PROVIDER=ollama` (default): `qwen3:14b` through Ollama's
  Anthropic-compatible API at `http://localhost:11434`. Runs fully offline.
- `LLM_PROVIDER=anthropic`: Claude through the Anthropic API; needs
  `ANTHROPIC_API_KEY` and internet.

`LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` override the defaults.

One-time Ollama setup (needs internet once, for the download):

```bash
ollama pull qwen3:14b
ollama create qwen3-14b-16k -f ollama/Modelfile   # 16k context; no download
python -m db_assistant.llm.client                 # check: prints the reply "hello"
```

Ollama's default context is 4096 tokens and it cuts longer prompts off
silently, so the app uses `qwen3-14b-16k` (built from `ollama/Modelfile`) and
refuses to start with a model whose context is smaller than
`OLLAMA_MIN_CONTEXT` in `config.py`.

Offline, nothing else needs the internet: the MCP server is a local process,
and Streamlit's usage statistics are switched off in `.streamlit/config.toml`
(run the UI from the project root so it is picked up).

## Run the UI

```bash
streamlit run db_assistant/ui/app.py
```

Opens a chat page in the browser. The sidebar has example questions (clear,
ambiguous, out of scope, sensitive, and one that triggers a repair) and a
"New conversation" button.

To see the setup errors, override `.env` for one run:

```bash
LLM_BASE_URL=http://127.0.0.1:9 streamlit run db_assistant/ui/app.py   # Ollama not running
LLM_MODEL=nope:1b streamlit run db_assistant/ui/app.py                 # model not installed
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY= streamlit run db_assistant/ui/app.py   # missing key
```

## Command line

```bash
python -m db_assistant.cli "How many orders were placed in July 2025?"
python -m db_assistant.cli --first-sql "SELECT SUM(revenue) FROM orders" "What is the total revenue?"
```

`--first-sql` replaces the LLM's first query, to force a repair.

## Tests and evaluation

```bash
pytest                                 # offline: no LLM calls, Ollama not needed
python tests/eval/run_eval.py          # configured LLM: answer quality + scope consistency
```

Eval failures are labelled `format` (reply wasn't the requested JSON or was
cut off), `judgment` (scope, clarification, broadening), `sql` (wrong rows,
failed after repairs), `answer` (expected text missing) or `setup`.

## MCP server

The server (`db_assistant/mcp_server/server.py`) exposes `list_tables`,
`describe_table` and `run_query` over stdio, and is registered for Claude Code
in `.mcp.json`. To inspect it by hand:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m db_assistant.mcp_server.server
```

The Inspector web UI (2.10-2.11) drops tool results with `isError: true`; check
rejected queries with its CLI mode instead (`--cli ... --method tools/call`).
