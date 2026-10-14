-- 004: record which postal codes people actually request.
--
-- The weekly refresh used to pick "postal codes scraped in the last 30 days",
-- but the refresh itself scrapes, so every postal code ever searched stayed
-- active forever. Demand is now recorded separately, by the API.

CREATE TABLE IF NOT EXISTS postal_code_activity (
    postal_code         VARCHAR(6)  PRIMARY KEY,
    first_requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_count       BIGINT      NOT NULL DEFAULT 1
);

-- Seed from the only demand evidence that predates this table: when each
-- legacy postal code was last scraped (each scrape was triggered by a user).
INSERT INTO postal_code_activity (postal_code, first_requested_at, last_requested_at)
SELECT postal_code, min(scraped_at)::timestamptz, max(scraped_at)::timestamptz
FROM flyer_items_legacy
WHERE postal_code IS NOT NULL
GROUP BY postal_code
ON CONFLICT (postal_code) DO NOTHING;
