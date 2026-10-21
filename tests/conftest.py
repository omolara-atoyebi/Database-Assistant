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
        self.calls.append({"system": system, "user_message": user_message})
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return self.replies.pop(0)


@pytest.fixture
def fake_llm():
    """Build a FakeLLM: fake_llm(["reply 1", "reply 2"])."""
    return FakeLLM
