-- 005: close Supabase's auto-generated Data API (PostgREST) over our tables.
--
-- Supabase publishes every table and view in "public" over HTTPS, reachable
-- with the project's anon key (which is meant to be embedded in frontends).
-- BudgetPlate never uses that API: the backend connects with DATABASE_URL
-- as the role that owns these tables. So the API gets nothing:
--
--   1. RLS on every table, with NO policies. Non-owner roles (anon,
--      authenticated) see zero rows. The owner bypasses RLS (it is not FORCEd),
--      so the backend, migrations and the GitHub Actions job are unaffected.
--   2. The flyer_items view runs with the CALLER's permissions. A view
--      normally runs as its owner, so an anon caller would read the tables
--      through it with the owner's rights and skip RLS entirely.
--   3. anon and authenticated lose every grant, now and for future tables, so
--      requests fail with "permission denied" before RLS is even consulted.
--
-- Every statement is idempotent. Step 3 only runs where those roles exist,
-- so on plain local Postgres it is a no-op.

-- 1. Row-level security, no policies.
ALTER TABLE dim_date               ENABLE ROW LEVEL SECURITY;
ALTER TABLE dim_store              ENABLE ROW LEVEL SECURITY;
ALTER TABLE dim_product            ENABLE ROW LEVEL SECURITY;
ALTER TABLE etl_batch              ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_price_observation ENABLE ROW LEVEL SECURITY;
ALTER TABLE dq_check_results       ENABLE ROW LEVEL SECURITY;
ALTER TABLE postal_code_activity   ENABLE ROW LEVEL SECURITY;
ALTER TABLE flyer_items_legacy     ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations      ENABLE ROW LEVEL SECURITY;

-- 2. The view checks the caller's privileges and RLS (Postgres 15+). Set in
-- place rather than re-running CREATE VIEW, so the definition in 003 stays
-- the only copy.
ALTER VIEW flyer_items SET (security_invoker = true);

-- 3. Supabase's API roles get no privileges, now or by default.
DO $$
DECLARE
    api_role text;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role);

        -- Existing objects ("ALL TABLES" includes views).
        EXECUTE format('REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM %I', api_role);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', api_role);

        -- Objects created later by this role (the one running migrations).
        -- Supabase's own grants live in the schema-level entry; the global
        -- one is revoked too in case a project sets it there.
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM %I', api_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', api_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES    FROM %I', api_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM %I', api_role);
    END LOOP;
END $$;
