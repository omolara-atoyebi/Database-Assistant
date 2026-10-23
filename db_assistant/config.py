"""All settings in one place. Other modules import from here."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The SQLite dataset. Always opened read-only.
DB_PATH = PROJECT_ROOT / "data" / "analytics.db"

# The only tables the MCP server will expose or let queries touch.
ALLOWED_TABLES = ("customers", "products", "orders", "order_items")

# Guardrail limits for run_query.
ROW_LIMIT = 200
QUERY_TIMEOUT_SECONDS = 5.0

# Rows to show for a "top"/"best" question that gives no number. The answer
# tells the user this default was used.
DEFAULT_TOP_N = 10

# Self-repair: how many times the LLM may revise failed SQL, and how many
# describe_table calls it may make within one repair.
MAX_REPAIRS = 2
MAX_TOOL_CALLS_PER_REPAIR = 3

# The runtime LLM. Can be overridden with the ANTHROPIC_MODEL environment variable.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Effort controls how much the model thinks (output_config.effort).
# Sonnet 5.5 defaults to "high"; the docs suggest "medium" or "low" for
# latency-sensitive work, and a user waits for each answer here.
# https://platform.claude.com/docs/en/build-with-claude/effort
EFFORT = "medium"

# Hard cap on output per request. Thinking counts toward it, so leave room.
MAX_TOKENS = 16000
