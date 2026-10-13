"""Test fixtures: every test runs against a disposable Postgres database.

Point TEST_DATABASE_URL elsewhere to override the default
(postgresql://localhost/budgetplate_test). The schema is dropped and rebuilt
from the migrations once per session, and every warehouse table is truncated
before each test.
"""

import os
from contextlib import closing
from urllib.parse import urlparse

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://localhost/budgetplate_test"
)

# Safety: this suite DROPs the public schema. Refuse anything that doesn't
# look like a throwaway test database.
_db_name = urlparse(TEST_DATABASE_URL).path.lstrip("/")
if "test" not in _db_name:
    raise RuntimeError(
        f"Refusing to run tests against database {_db_name!r}: its name must "
        "contain 'test' because the suite drops and recreates the schema."
    )

# Must be set before config.py is imported (Settings reads env at import time).
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import psycopg2  # noqa: E402
import pytest  # noqa: E402

import migrate  # noqa: E402

# Static reference data that tests never modify.
_KEEP_TABLES = {"dim_date", "schema_migrations"}


def _connect():
    return closing(psycopg2.connect(TEST_DATABASE_URL))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        conn.commit()
    migrate.apply_all()
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            tables = [t for (t,) in cur.fetchall() if t not in _KEEP_TABLES]
            if tables:
                cur.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest.fixture
def db():
    """A raw connection for assertions. Rolled back and closed after the test."""
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()

