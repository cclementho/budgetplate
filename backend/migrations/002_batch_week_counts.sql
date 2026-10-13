-- 002: per-flyer-week row counts on each batch.
--
-- One scrape can span several flyer weeks: a long-running promo from earlier
-- weeks, and on a Wednesday often next week's flyer too (the July V7C4K2
-- scrape held two full weeks). Total rows per batch are therefore not
-- comparable batch to batch; rows in each batch's dominant week are.

ALTER TABLE etl_batch ADD COLUMN IF NOT EXISTS week_counts JSONB;

COMMENT ON COLUMN etl_batch.week_counts IS
    'Fact rows per flyer_week_start in this batch, e.g. {"2026-10-08": 861}.';
