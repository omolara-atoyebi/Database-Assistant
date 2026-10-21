# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## What we're building

"Database Assistant": a natural-language-to-SQL assistant over a SQLite
analytics database. A user asks a question in a UI and gets a natural-language
answer with the executed SQL cited.

Two different roles — keep them separate:
- **Claude Code (you)** is the development tool that helps build this project.
  You are not part of the product.
- **The runtime LLM** (currently Claude via the Anthropic API) is a component
  inside the app. It handles scope checks, clarification, SQL generation and
  repair. Keep the LLM call behind one module so the model could be swapped.

The deliverable is the app: UI → backend pipeline → Claude API + MCP server
→ `data/analytics.db`.

## Project state

No application code, build system or test suite yet. The repo holds the
dataset (`data/analytics.db`) and the design diagram (`docs/process_flow.png`).
Don't invent build/test commands; add them here once tooling exists.

## Pipeline

The classic text-to-SQL stages, mapped to `docs/process_flow.png`:

| Stage    | In the diagram                                         |
|----------|--------------------------------------------------------|
| Parse    | Scope gate + clarify if ambiguous                      |
| Map      | Schema context + few-shot examples (schema linking)    |
| Generate | Claude generates SQL; extract a clean query            |
| Execute  | Guardrail validation + execute in the MCP server       |
| Format   | Natural-language answer + cited SQL                    |
| Result   | Check result → self-repair loop                        |

This is a **workflow**: application code controls the order of steps.

## Agentic behaviours

Four points where runtime llm makes the decision. These must be LLM judgments,
not hard-coded rules:

1. **Scope gate**: runtime llm classifies the question as in_scope /
   out_of_scope / sensitive, with a reason. Out-of-scope or sensitive →
   decline/redirect.
2. **Clarification**: runtime llm decides whether the question is ambiguous
   (e.g. "best customers": revenue or order count?) and, if so, writes the
   clarifying question for the user.
3. **Self-repair**: on validation failure, execution error or empty result,
   runtime llm receives the error and revises the SQL. Empty result: check types
   first, then broaden scope and notify the user. Max 2 repairs.
4. **Tool use within repair**: during self-repair, runtime llm may call
   `describe_table` itself (via API tool use) to re-check the schema before
   revising. Tool use is only available inside the repair loop.

## MCP server

A small local MCP server, built in this repo, wrapping `data/analytics.db`.
Read-only tools: `list_tables`, `describe_table`, `run_query`.

Guardrails are enforced **in the server**, not the prompt: SELECT-only, table
whitelist, column validation, row limit, timeout. The prompt can ask for safe
SQL, but the server must reject unsafe SQL regardless.

## Working with the database

```bash
sqlite3 -header -column data/analytics.db "SELECT ..."
sqlite3 data/analytics.db ".schema"
```

Treat `analytics.db` as a read-only source dataset. Don't write to it unless
explicitly asked.

### Schema (small e-commerce dataset)

- `customers(id, name, country, signup_date)`: ~30 rows; `country` is a
  2-letter code: US, UK, FR, SE, FI, DE (note `UK`, not `GB`).
- `products(id, name, category, price)`: 10 rows; categories: Books, Games,
  Electronics, Home, Clothing.
- `orders(id, customer_id → customers.id, order_date, total_amount)`: 300
  rows, dates 2025-04-11 to 2025-10-08.
- `order_items(order_id → orders.id, product_id → products.id, quantity,
  unit_price)`: composite PK `(order_id, product_id)`.

### Query notes (these are the traps the few-shot examples must cover)

- Dates are `TEXT` in ISO `YYYY-MM-DD`; use `strftime('%Y-%m', order_date)` /
  `date(...)` for bucketing and comparisons.
- `orders.total_amount` equals `SUM(quantity * unit_price)` of its
  `order_items`. Joining `orders` to `order_items` and summing `total_amount`
  double-counts; use `order_items` for product/category breakdowns.
- Use `order_items.unit_price` (price at sale) for revenue, not
  `products.price`, even though they currently match.

## Stack

- Python 3, built-in `sqlite3`, Anthropic Python SDK, MCP Python SDK
- Anthropic API key in `.env` (never commit it; `.env` is in `.gitignore`)
- Simple UI last (Streamlit or similar)

## How to work with me

- I'm learning agentic coding. Before writing code, explain your plan in
  plain language and wait for my approval.
- Build one phase at a time. Don't start the next phase until I say so.
- After each change, tell me which files changed and why, and how to run/test it.
- Write tests for guardrails (e.g. DROP/UPDATE rejected, row limit enforced).
- Keep code simple and readable over clever.
- When I correct a mistake, suggest a line to add to this file.