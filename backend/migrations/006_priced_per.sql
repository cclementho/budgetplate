-- 006: remember when a flyer price is a per-weight rate ("$2.00/lb").
--
-- Flipp's flyer list gives such items a bare price of 2.00, so a pork roast at
-- $2.00/lb used to be stored, and planned, as a $2.00 item. The scraper now
-- reads the unit from Flipp's item detail and the parser sets priced_per:
--   lb | kg | 100g   the price is per that unit; price_per_kg holds the
--                    converted rate and unit_type is 'kg'
--   NULL             priced each / per pack, as before (all existing rows)

ALTER TABLE fact_price_observation
    ADD COLUMN IF NOT EXISTS priced_per TEXT
    CONSTRAINT ck_priced_per CHECK (priced_per IN ('lb', 'kg', '100g'));

COMMENT ON COLUMN fact_price_observation.priced_per IS
    'lb, kg or 100g when price is a per-weight rate; NULL when priced each.';

-- Same view as 003 with priced_per added as the last column (CREATE OR REPLACE
-- can only append columns). security_invoker is restated so the view keeps
-- checking the caller's rights (005).
CREATE OR REPLACE VIEW flyer_items WITH (security_invoker = true) AS
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
    (lb.finished_at AT TIME ZONE 'UTC')  AS scraped_at,
    f.priced_per
FROM fact_price_observation f
JOIN latest_batch lb ON lb.batch_id = f.last_batch_id
JOIN dim_store   s   ON s.store_key   = f.store_key
JOIN dim_product p   ON p.product_key = f.product_key;
