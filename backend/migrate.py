"""Minimal SQL migration runner (no Alembic, no extra infrastructure).

Applies ``migrations/NNN_*.sql`` in filename order and records each one in
``schema_migrations`` so it runs exactly once per database.

All pending migrations run inside ONE transaction that first takes a
transaction-scoped advisory lock. That gives two guarantees:

* Atomicity: Postgres DDL is transactional, so a failing migration rolls back
  everything in the run and leaves the database as it was.
* No double-apply: the Render web app (on startup) and the GitHub Actions
  weekly job can both call ``apply_all()`` at the same moment; the second one
  waits on the lock and then sees nothing pending. A transaction-scoped lock
  (rather than a session lock) is used because Supabase's transaction-mode
  pooler drops session state between transactions.

Usage:  python migrate.py
"""

import logging
import os

from database import get_conn

logger = logging.getLogger("budgetplate.migrate")

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")

# Arbitrary constant that identifies "BudgetPlate schema migrations" in
# pg_advisory_xact_lock. Any process applying migrations takes this lock.
_MIGRATION_LOCK_KEY = 4_170_301


def migration_files() -> list[str]:
    """All migration filenames, in the order they must be applied."""
    return sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))


def apply_all() -> list[str]:
    """Apply every pending migration. Returns the filenames that were applied."""
    applied_now: list[str] = []
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        filename   TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute("SELECT filename FROM schema_migrations")
                already_applied = {row[0] for row in cur.fetchall()}

                for name in migration_files():
                    if name in already_applied:
                        continue
                    with open(os.path.join(MIGRATIONS_DIR, name)) as f:
                        cur.execute(f.read())
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)", (name,)
                    )
                    applied_now.append(name)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for name in applied_now:
        logger.info("Applied migration %s", name)
    return applied_now


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ran = apply_all()
    print(f"Applied {len(ran)} migration(s): {', '.join(ran) or 'none pending'}")
