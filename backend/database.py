"""PostgreSQL access layer for BudgetPlate.

Uses a small psycopg2 connection pool. Keeping this thin and dependency-light
makes the same code work against a local Postgres or a hosted Supabase instance
(just change ``DATABASE_URL``).
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

from config import settings

_pool: SimpleConnectionPool | None = None


def init_pool() -> None:
    """Create the connection pool (idempotent)."""
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(1, 10, dsn=settings.DATABASE_URL)


@contextmanager
def get_conn():
    """Yield a pooled connection, returning it to the pool afterwards."""
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor(commit: bool = False):
    """Yield a dict cursor. Commits on success when ``commit`` is True."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def init_db() -> None:
    """Apply schema.sql so the app works on a fresh database."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r") as f:
        schema_sql = f.read()
    with get_cursor(commit=True) as cur:
        cur.execute(schema_sql)
