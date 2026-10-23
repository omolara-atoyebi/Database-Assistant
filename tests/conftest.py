"""Shared pytest fixtures."""

import sqlite3

import pytest

from db_assistant import config


@pytest.fixture
def db_connection():
    """A read-only connection to the real dataset, closed after each test."""
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    yield conn
    conn.close()


class FakeLLM:
    """Stand-in for LLMClient: returns scripted replies, records every call.

    Pipeline tests use it so they never call the real API.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user_message):
        return self._next({"system": system, "user_message": user_message})

    def complete_json(self, system, user_message, schema):
        # Scripted replies for this method are dicts, as the real client returns.
        return self._next({"system": system, "user_message": user_message, "schema": schema})

    def complete_with_tools(self, system, user_message, tools, call_tool, schema, max_tool_calls):
        """Scripted reply: a dict, optionally with "tool_calls": [{"name", "input"}].

        The scripted tool calls are really run through call_tool (so through
        the MCP server), and their outcomes are recorded in the call.
        """
        call = {
            "system": system,
            "user_message": user_message,
            "schema": schema,
            "tools": tools,
            "tool_outcomes": [],
        }
        scripted = dict(self._next(call))
        tool_calls = []
        for tool_call in scripted.pop("tool_calls", []):
            outcome = call_tool(tool_call["name"], tool_call["input"])
            call["tool_outcomes"].append(outcome)
            tool_calls.append({**tool_call, "is_error": outcome["is_error"]})
        return scripted, tool_calls

    def _next(self, call):
        self.calls.append(call)
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return self.replies.pop(0)


@pytest.fixture
def fake_llm():
    """Build a FakeLLM: fake_llm(["reply 1", "reply 2"])."""
    return FakeLLM
