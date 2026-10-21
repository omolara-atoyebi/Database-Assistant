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

# The runtime LLM. Can be overridden with the ANTHROPIC_MODEL environment variable.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Effort controls how much the model thinks (output_config.effort).
# Sonnet 5.5 defaults to "high"; the docs suggest "medium" or "low" for
# latency-sensitive work, and a user waits for each answer here.
# https://platform.claude.com/docs/en/build-with-claude/effort
EFFORT = "medium"

# Hard cap on output per request. Thinking counts toward it, so leave room.
MAX_TOKENS = 16000
