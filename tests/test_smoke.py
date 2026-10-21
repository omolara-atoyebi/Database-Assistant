"""Smoke tests: the package imports and the config matches the real database."""

from db_assistant import config


def test_database_file_exists():
    assert config.DB_PATH.is_file()


def test_allowed_tables_match_database(db_connection):
    rows = db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    assert sorted(name for (name,) in rows) == sorted(config.ALLOWED_TABLES)
