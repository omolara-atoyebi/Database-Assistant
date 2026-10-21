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
