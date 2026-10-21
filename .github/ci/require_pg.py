"""Fail before pytest falls back to SQLite. Source: tests_py/conftest.py."""

import os

import psycopg

with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
    # source: existing test-suite/action.yml provisions these two extensions.
    connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    extensions = {
        row[0] for row in connection.execute("SELECT extname FROM pg_extension")
    }
    required = {"vector", "pg_trgm"}
    if not required <= extensions:
        raise RuntimeError(f"Missing PostgreSQL extensions: {required - extensions}")
print("PostgreSQL, pgvector and pg_trgm available")
