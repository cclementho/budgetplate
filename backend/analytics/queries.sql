-- BudgetPlate price-history analytics.
--
-- Plain SQL against the star schema (no psql meta-commands), so each query
-- runs as-is in psql, the Supabase SQL editor or any client. Each starts with
-- a "-- name:" line; tests/test_analytics.py runs them by name.
--
-- Unit price: price per kg when a weight was parsed, else per pack unit or
-- per item (fact_price_observation.unit_type). Comparisons always stay
-- within one product (same chain, name and size), so units never mix.
--
-- Ambiguous rows: when a flyer listed one product name at several prices in
-- the same week (likely different variants), the fact keeps the lowest price
-- and is_ambiguous = true. Queries that compare a price to that product's
-- "usual" price (Q1-Q3) exclude those rows, because a cheaper variant would
-- look like a discount. Q4 keeps them: a median over many rows isn't moved
-- by a few variant mismatches.


-- name: q1_chain_discount_frequency
-- Business question: which chain discounts most often?
-- For every product a chain has advertised in at least two weeks, its "usual
-- price" is the median unit price across all those weeks. A week counts as a
-- discount when the price is at least 5% below that usual price (smaller moves
-- are noise). Also shows how many items each chain advertises per week.
-- Caveat: with only a few weeks of history this is noisy; it settles after
-- roughly six weeks of data.
WITH obs AS (
    SELECT f.product_key, f.store_key, f.flyer_week_start, f.unit_price
    FROM fact_price_observation f
    WHERE NOT f.is_ambiguous AND f.unit_price IS NOT NULL
),
usual AS (
    SELECT product_key, store_key,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY unit_price) AS usual_price
    FROM obs
    GROUP BY product_key, store_key
    HAVING count(*) >= 2
),
discounts AS (
    SELECT s.chain,
           count(*) AS observations_with_history,
           count(*) FILTER (WHERE o.unit_price < u.usual_price * 0.95) AS discounted,
           avg(1 - o.unit_price / u.usual_price)
               FILTER (WHERE o.unit_price < u.usual_price * 0.95) AS avg_depth
    FROM obs o
    JOIN usual u USING (product_key, store_key)
    JOIN dim_store s USING (store_key)
    GROUP BY s.chain
),
volume AS (
    SELECT s.chain,
           count(*)::numeric / count(DISTINCT (f.store_key, f.flyer_week_start))
               AS items_per_flyer_week
    FROM fact_price_observation f
    JOIN dim_store s USING (store_key)
    GROUP BY s.chain
)
SELECT v.chain,
       round(v.items_per_flyer_week, 0)                                     AS items_per_flyer_week,
       coalesce(d.observations_with_history, 0)                             AS observations_with_history,
       coalesce(d.discounted, 0)                                            AS discounted,
       round(100.0 * d.discounted / nullif(d.observations_with_history, 0), 1) AS discount_rate_pct,
       round((100 * d.avg_depth)::numeric, 1)                               AS avg_discount_depth_pct
FROM volume v
LEFT JOIN discounts d USING (chain)
ORDER BY discount_rate_pct DESC NULLS LAST, v.chain;


-- name: q2_week_over_week_common_items
-- Business question: how did prices of staples (chicken, eggs, rice) change
-- since the last time each product was advertised?
-- Compares each observation with the same product's previous observation at
-- the same chain and market. Products aren't on flyer every week, so
-- weeks_apart shows the real gap (1 = consecutive weeks). Ambiguous rows are
-- excluded, so "previous" means the previous unambiguous observation.
-- \m and \M are Postgres word boundaries: "rice" never matches "price".
WITH obs AS (
    SELECT s.market_postal_code, p.chain, p.display_name, f.unit_type,
           f.product_key, f.store_key, f.flyer_week_start, f.unit_price
    FROM fact_price_observation f
    JOIN dim_product p USING (product_key)
    JOIN dim_store s USING (store_key)
    WHERE NOT f.is_ambiguous
      AND f.unit_price IS NOT NULL
      AND p.normalized_name ~ '\m(chicken|eggs?|rice)\M'
),
changes AS (
    SELECT *,
           lag(unit_price)       OVER w AS prev_unit_price,
           lag(flyer_week_start) OVER w AS prev_week
    FROM obs
    WINDOW w AS (PARTITION BY product_key, store_key ORDER BY flyer_week_start)
)
SELECT market_postal_code,
       chain,
       display_name,
       unit_type,
       prev_week,
       flyer_week_start                                                AS week,
       (flyer_week_start - prev_week) / 7                              AS weeks_apart,
       prev_unit_price,
       unit_price,
       round(100.0 * (unit_price - prev_unit_price) / prev_unit_price, 1) AS pct_change
FROM changes
WHERE prev_unit_price IS NOT NULL
ORDER BY abs(unit_price - prev_unit_price) / prev_unit_price DESC, display_name;


-- name: q3_share_of_real_deals
-- Business question: how many of this week's flyer "deals" are actually below
-- what that product usually costs?
-- Point-in-time: each week is judged only against the product's EARLIER
-- weeks (median of prior unit prices), which is what a shopper could have
-- known then. No look-ahead. Products with no earlier history can't be
-- judged and are counted separately.
WITH obs AS (
    SELECT f.product_key, f.store_key, f.flyer_week_start, f.unit_price
    FROM fact_price_observation f
    WHERE NOT f.is_ambiguous AND f.unit_price IS NOT NULL
),
judged AS (
    SELECT o.*, prior.usual_before
    FROM obs o
    -- Reads the base table, not the obs CTE: a CTE referenced twice is
    -- materialized and would be rescanned per row (O(n^2)), while this lookup
    -- uses the (product_key, store_key, flyer_week_start) unique index.
    LEFT JOIN LATERAL (
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY h.unit_price) AS usual_before
        FROM fact_price_observation h
        WHERE h.product_key = o.product_key
          AND h.store_key = o.store_key
          AND h.flyer_week_start < o.flyer_week_start
          AND NOT h.is_ambiguous
          AND h.unit_price IS NOT NULL
    ) prior ON true
)
SELECT s.market_postal_code,
       j.flyer_week_start                                           AS week,
       count(*)                                                     AS advertised,
       count(j.usual_before)                                        AS with_prior_history,
       count(*) FILTER (WHERE j.unit_price < j.usual_before)        AS below_usual,
       count(*) FILTER (WHERE j.unit_price = j.usual_before)        AS same_as_usual,
       count(*) FILTER (WHERE j.unit_price > j.usual_before)        AS above_usual,
       round(100.0 * count(*) FILTER (WHERE j.unit_price < j.usual_before)
             / nullif(count(j.usual_before), 0), 1)                 AS pct_real_deals
FROM judged j
JOIN dim_store s USING (store_key)
GROUP BY s.market_postal_code, j.flyer_week_start
ORDER BY s.market_postal_code, week;


-- name: q4_category_price_trends
-- Business question: are categories getting cheaper or pricier over time?
-- Median price per kg by category, market and flyer week, using only
-- products sold by weight so the units are comparable. Groups with fewer than
-- 5 products are hidden as too thin to call a trend. pct_change compares with
-- the same category's previous week shown for that market.
WITH weekly AS (
    SELECT s.market_postal_code,
           f.flyer_week_start                                         AS week,
           p.category,
           count(*)                                                   AS products,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY f.unit_price)  AS median_per_kg
    FROM fact_price_observation f
    JOIN dim_product p USING (product_key)
    JOIN dim_store s USING (store_key)
    WHERE f.unit_type = 'kg' AND f.unit_price IS NOT NULL
    GROUP BY s.market_postal_code, f.flyer_week_start, p.category
    HAVING count(*) >= 5
)
SELECT market_postal_code,
       category,
       week,
       products,
       round(median_per_kg::numeric, 2) AS median_price_per_kg,
       round((100 * (median_per_kg - lag(median_per_kg) OVER w)
              / lag(median_per_kg) OVER w)::numeric, 1) AS pct_change
FROM weekly
WINDOW w AS (PARTITION BY market_postal_code, category ORDER BY week)
ORDER BY market_postal_code, category, week;
