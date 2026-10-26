"""All settings in one place. Other modules import from here."""

import os
from pathlib import Path

from dotenv import load_dotenv

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

# Rows of a query result the answer step sees. The UI still shows every row;
# the answer step is told how many it didn't see.
ANSWER_ROW_LIMIT = 50

# --- The runtime LLM ---------------------------------------------------------
# Chosen in .env, so switching provider needs no code change:
#   LLM_PROVIDER=ollama      a local model through Ollama's Anthropic-compatible
#                            API; runs fully offline (the default)
#   LLM_PROVIDER=anthropic   Claude through the Anthropic API (needs internet)
# LLM_BASE_URL, LLM_API_KEY and LLM_MODEL override the provider's defaults.
# For anthropic the key defaults to ANTHROPIC_API_KEY.

OLLAMA = "ollama"
ANTHROPIC = "anthropic"

load_dotenv(PROJECT_ROOT / ".env")  # real environment variables win over .env

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", OLLAMA).strip().lower()
if LLM_PROVIDER not in (OLLAMA, ANTHROPIC):
    raise ValueError(f"LLM_PROVIDER must be '{OLLAMA}' or '{ANTHROPIC}', got '{LLM_PROVIDER}'.")

_DEFAULTS = {
    # qwen3-14b-16k is qwen3:14b with a 16k context, built from ollama/Modelfile.
    OLLAMA: {"base_url": "http://localhost:11434", "api_key": "ollama", "model": "qwen3-14b-16k"},
    ANTHROPIC: {"base_url": None, "api_key": os.environ.get("ANTHROPIC_API_KEY"), "model": "claude-sonnet-5-5"},
}
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or _DEFAULTS[LLM_PROVIDER]["base_url"]
LLM_API_KEY = os.environ.get("LLM_API_KEY") or _DEFAULTS[LLM_PROVIDER]["api_key"]
MODEL = os.environ.get("LLM_MODEL") or _DEFAULTS[LLM_PROVIDER]["model"]

# Anthropic only. Effort controls how much the model thinks
# (output_config.effort). Sonnet 5.5 defaults to "high"; the docs suggest
# "medium" or "low" for latency-sensitive work, and a user waits for each answer.
# https://platform.claude.com/docs/en/build-with-claude/effort
# Ollama ignores effort; there, thinking is switched on or off per step (llm/client.py).
EFFORT = "medium"

# Ollama only. Ollama's default context is 4096 tokens and it cuts longer
# prompts off silently. The model must be built with at least this num_ctx
# (ollama/Modelfile); the client checks this at startup, and refuses any
# request whose prompt plus MAX_TOKENS might not fit.
OLLAMA_MIN_CONTEXT = 16384

# Hard cap on output per request. Thinking counts toward it, so leave room.
# On Ollama the reply shares the context window with the prompt.
MAX_TOKENS = 16000 if LLM_PROVIDER == ANTHROPIC else 6144
