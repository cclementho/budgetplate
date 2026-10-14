"""Migration 005 closes Supabase's Data API without affecting the backend.

Local Postgres has no Supabase roles, so the role tests recreate the relevant
part of a Supabase project in the test database:

* ``anon`` / ``authenticated`` with the grants a new Supabase project gives
  them (everything in public, now and by default);
* a non-superuser table owner standing in for Supabase's ``postgres`` role,
  which is what the backend connects as. (The local test user is a superuser,
  and superusers skip RLS, so testing as it would prove nothing.)

Roles are cluster-wide, so they are created only if absent and always dropped
afterwards.
"""

import os

import psycopg2
import pytest
from psycopg2 import errors
from psycopg2.pool import SimpleConnectionPool

import database
import queries
from scraper.warehouse import load_batch
from tests.helpers import current_row, parsed, query, scalar

PC = "M5S1Z9"
OWNER = "budgetplate_test_owner"
API_ROLES = ("anon", "authenticated")
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "migrations",
                         "005_lock_down_data_api.sql")

SUPABASE_DEFAULT_GRANTS = """
    GRANT USAGE ON SCHEMA public TO anon, authenticated;
    GRANT ALL ON ALL TABLES    IN SCHEMA public TO anon, authenticated;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES    TO anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;
"""


def _relations(cur, owner=None):
    """(name, 'TABLE' | 'VIEW') for every table and view in public."""
    cur.execute(
        """
        SELECT c.relname, CASE c.relkind WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')
          AND (%s IS NULL OR c.relowner = %s::regrole)
        ORDER BY 1
        """,
        (owner, owner),
    )
    return cur.fetchall()


def _set_owner(cur, relations, owner):
    for name, kind in relations:
        cur.execute(f'ALTER {kind} "{name}" OWNER TO {owner}')


@pytest.fixture(scope="module")
def supabase_roles(_schema):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                ([*API_ROLES, OWNER],))
    if cur.fetchall():
        conn.close()
        pytest.skip(f"roles {API_ROLES + (OWNER,)} already exist in this cluster; "
                    "not touching roles this suite didn't create")
    try:
        cur.execute(f"CREATE ROLE {OWNER} NOLOGIN NOSUPERUSER NOBYPASSRLS")
    except errors.InsufficientPrivilege:
        conn.close()
        pytest.skip("the test database user can't create roles")

    try:
        cur.execute("CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN")
        cur.execute(SUPABASE_DEFAULT_GRANTS)
        # Re-run 005 now that the roles exist (it already ran, as a no-op for
        # step 3, when the session built the schema). Also proves it's rerunnable.
        with open(MIGRATION) as f:
            cur.execute(f.read())
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {OWNER}")
        _set_owner(cur, _relations(cur), OWNER)
        yield
    finally:
        _set_owner(cur, _relations(cur, owner=OWNER), "CURRENT_USER")
        cur.execute(f"DROP OWNED BY anon, authenticated, {OWNER}")
        cur.execute(f"DROP ROLE IF EXISTS anon, authenticated, {OWNER}")
        conn.close()


@pytest.fixture
def backend_as_owner(supabase_roles, monkeypatch):
    """Route the backend's own connection pool through the non-superuser owner."""
    pool = SimpleConnectionPool(1, 2, dsn=os.environ["DATABASE_URL"],
                                options=f"-c role={OWNER}")
    monkeypatch.setattr(database, "_pool", pool)
    yield
    pool.closeall()


def _sample_week():
    load_batch(PC, parsed(
        current_row("BOK CHOY", "1.99"),
        current_row("PORK BELLY, 1 kg", "6.99", merchant="FreshCo"),
    ))


# --- the migration's effect on the schema (no roles needed) ------------------

def test_every_table_in_public_has_rls_enabled_but_not_forced(db):
    tables = query(db, """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'""")
    assert len(tables) == 9
    # A future migration that adds a table without RLS fails here.
    assert [t["relname"] for t in tables if not t["relrowsecurity"]] == []
    # FORCE would subject the owner (the backend) to RLS too.
    assert [t["relname"] for t in tables if t["relforcerowsecurity"]] == []


def test_flyer_items_view_runs_with_the_callers_rights(db):
    options = scalar(db, "SELECT reloptions FROM pg_class WHERE relname = 'flyer_items'")
    assert "security_invoker=true" in options


# --- the backend, connected as a non-superuser owner like Supabase's postgres --

def test_backend_connection_still_loads_and_reads_flyer_items(backend_as_owner):
    with database.get_cursor() as cur:
        cur.execute("SELECT current_user AS who, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone() == {"who": OWNER, "rolsuper": False, "rolbypassrls": False}

    _sample_week()  # writes through RLS-enabled tables, as the backend

    assert sorted(i["clean_name"] for i in queries.get_items(PC)) == ["Bok Choy", "Pork Belly"]
    assert queries.get_deal_counts(PC) == {"No Frills": 1, "FreshCo": 1}
    assert queries.get_postal_meta(PC)["item_count"] == 2


# --- Supabase's API roles ------------------------------------------------------

@pytest.mark.parametrize("role", API_ROLES)
def test_api_roles_are_denied_every_table_and_view(db, supabase_roles, role):
    _sample_week()
    with db.cursor() as cur:
        relations = _relations(cur)
    assert ("flyer_items", "VIEW") in relations

    for name, _ in relations:
        with db.cursor() as cur:
            cur.execute(f"SET ROLE {role}")
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(f'SELECT 1 FROM "{name}" LIMIT 1')
        db.rollback()  # also undoes SET ROLE


def test_rls_still_hides_rows_if_select_is_granted_again(db, supabase_roles):
    """Defence in depth: privileges and RLS each block the API on their own."""
    _sample_week()
    with db.cursor() as cur:
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon")
        cur.execute("SET ROLE anon")
        assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 0
        assert scalar(db, "SELECT count(*) FROM flyer_items") == 0

        # Why the view must be security_invoker: without it, the view reads
        # the tables as their owner, and the owner isn't subject to RLS.
        cur.execute("RESET ROLE")
        cur.execute("ALTER VIEW flyer_items SET (security_invoker = false)")
        cur.execute("SET ROLE anon")
        assert scalar(db, "SELECT count(*) FROM flyer_items") == 2
    db.rollback()


def test_tables_created_later_are_not_exposed(db, supabase_roles):
    with db.cursor() as cur:
        cur.execute("CREATE TABLE lockdown_probe (id int)")
        for role in API_ROLES:
            cur.execute("SELECT has_table_privilege(%s, 'lockdown_probe', 'SELECT')", (role,))
            assert cur.fetchone()[0] is False
    db.rollback()
