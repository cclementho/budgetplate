-- 003: retire the overwrite-in-place flyer_items table; serve the API from
-- the warehouse through a view with the same name and columns.
--
-- queries.py and every route keep reading "flyer_items" unchanged. The old
-- table is renamed, not dropped: its rows are the backfill source (backfill.py)
-- and it doubles as a safety copy.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname = 'flyer_items'
          AND c.relkind = 'r'          -- an ordinary table, i.e. not yet migrated
    ) THEN
        ALTER TABLE flyer_items RENAME TO flyer_items_legacy;
    END IF;
END $$;

-- For each postal code: exactly the rows its most relevant successful batch
-- saw. That reproduces the old table's semantics (it held one scrape per
-- postal code) without double-counting a product across weeks.
--
-- "Most relevant" = newest flyer week, then most recently finished. Ordering
-- by load time alone would let a backfill of July data, run today, hide
-- today's scrape.
CREATE OR REPLACE VIEW flyer_items AS
WITH latest_batch AS (
    SELECT DISTINCT ON (postal_code) batch_id, finished_at
    FROM etl_batch
    WHERE status IN ('success', 'success_with_warnings')
      AND loaded_row_count > 0
    ORDER BY postal_code, max_flyer_week DESC, finished_at DESC, batch_id DESC
)
SELECT
    f.observation_key                    AS id,
    s.market_postal_code                 AS postal_code,
    s.chain                              AS merchant,
    f.original_name,
    p.display_name                       AS clean_name,
    p.category,
    f.price,
    f.price_per_kg,
    f.price_per_unit,
    f.weight_kg,
    -- The API compares these with naive UTC datetimes (datetime.utcnow()),
    -- so expose naive UTC rather than server-local time.
    (f.valid_from   AT TIME ZONE 'UTC')  AS valid_from,
    (f.valid_to     AT TIME ZONE 'UTC')  AS valid_to,
    (lb.finished_at AT TIME ZONE 'UTC')  AS scraped_at
FROM fact_price_observation f
JOIN latest_batch lb ON lb.batch_id = f.last_batch_id
JOIN dim_store   s   ON s.store_key   = f.store_key
JOIN dim_product p   ON p.product_key = f.product_key;

COMMENT ON VIEW flyer_items IS
    'Compatibility view for the API: the latest successful batch per postal code. '
    'History lives in fact_price_observation.';
