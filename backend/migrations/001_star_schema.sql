-- 001: price-history warehouse (star schema).
--
-- Grain of the fact table: ONE product at ONE store-market in ONE flyer week.
-- Every scrape is recorded as an etl_batch; facts are upserted on the grain so
-- re-loading the same postal code + flyer week never duplicates rows.
-- Everything here is plain PostgreSQL (15+), no extensions, so it runs the same
-- on local Postgres and on Supabase.

-- ---------------------------------------------------------------------------
-- dim_date: one row per calendar day (role-playing: flyer week, valid from/to)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_date (
    date_key          DATE PRIMARY KEY,
    flyer_week_start  DATE     NOT NULL,  -- Thursday on/before date_key
    iso_year          SMALLINT NOT NULL,
    iso_week          SMALLINT NOT NULL,
    year              SMALLINT NOT NULL,
    quarter           SMALLINT NOT NULL,
    month             SMALLINT NOT NULL,
    month_name        TEXT     NOT NULL,
    day_name          TEXT     NOT NULL
);

COMMENT ON COLUMN dim_date.flyer_week_start IS
    'Thursday on/before this date. Canadian grocery flyers run Thursday to Wednesday.';

-- ISODOW: Mon=1 .. Thu=4 .. Sun=7. (isodow - 4 + 7) % 7 = days since Thursday.
INSERT INTO dim_date (date_key, flyer_week_start, iso_year, iso_week, year,
                      quarter, month, month_name, day_name)
SELECT d::date,
       (d::date - ((EXTRACT(ISODOW FROM d)::int - 4 + 7) % 7))::date,
       EXTRACT(ISOYEAR FROM d)::smallint,
       EXTRACT(WEEK FROM d)::smallint,
       EXTRACT(YEAR FROM d)::smallint,
       EXTRACT(QUARTER FROM d)::smallint,
       EXTRACT(MONTH FROM d)::smallint,
       to_char(d, 'FMMonth'),
       to_char(d, 'FMDay')
FROM generate_series('2020-01-01'::date, '2035-12-31'::date, interval '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- dim_store: a chain within a postal-code market
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_store (
    store_key           SERIAL PRIMARY KEY,
    chain               TEXT       NOT NULL,
    market_postal_code  VARCHAR(6) NOT NULL,
    first_seen_week     DATE NOT NULL REFERENCES dim_date (date_key),
    last_seen_week      DATE NOT NULL REFERENCES dim_date (date_key),
    CONSTRAINT uq_dim_store UNIQUE (chain, market_postal_code)
);

COMMENT ON TABLE dim_store IS
    'A chain within a postal-code market. Flipp serves flyers per postal code, '
    'not per physical store, so this is not a physical location.';

-- ---------------------------------------------------------------------------
-- dim_product: chain-scoped product, matched by normalized name + size
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_product (
    product_key      SERIAL PRIMARY KEY,
    chain            TEXT          NOT NULL,
    normalized_name  TEXT          NOT NULL,  -- matching key
    display_name     TEXT          NOT NULL,  -- latest cleaned name seen
    category         VARCHAR(50)   NOT NULL,
    weight_kg        NUMERIC(10,3),           -- size part of the key (NULL = no size)
    pack_count       INT,                     -- size part of the key (NULL = not a pack)
    size_unit        TEXT,                    -- unit as printed: g, kg, ml, l, lb
    first_seen_week  DATE NOT NULL REFERENCES dim_date (date_key),
    last_seen_week   DATE NOT NULL REFERENCES dim_date (date_key),
    -- NULLS NOT DISTINCT (PG15+): "no size" must match "no size" across weeks;
    -- a plain UNIQUE would treat every NULL as different and create duplicates.
    CONSTRAINT uq_dim_product_natural
        UNIQUE NULLS NOT DISTINCT (chain, normalized_name, weight_kg, pack_count)
);

COMMENT ON TABLE dim_product IS
    'Same product across weeks = same chain + normalized name + size. Renamed '
    'items or sizes hidden in odd formats will not match (see README limitations).';

-- ---------------------------------------------------------------------------
-- etl_batch: append-only audit log, one row per load attempt
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_batch (
    batch_id                 BIGSERIAL PRIMARY KEY,
    postal_code              VARCHAR(6) NOT NULL,
    source                   TEXT NOT NULL CHECK (source IN ('scrape', 'backfill')),
    triggered_by             TEXT NOT NULL,   -- on_demand | scheduled | github_actions | manual | backfill
    status                   TEXT NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'success',
                                               'success_with_warnings', 'failed')),
    started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at              TIMESTAMPTZ,
    -- Row accounting (each later stage is <= the one before it).
    raw_row_count            INT,  -- rows the scraper returned
    priced_row_count         INT,  -- rows handed to the loader (had a price string)
    nonpositive_price_count  INT,  -- of those, price missing or <= 0 after parsing
    dropped_row_count        INT,  -- rows discarded by the loader (price, dates)
    usable_row_count         INT,  -- priced - dropped
    loaded_row_count         INT,  -- fact rows written (distinct grain keys)
    inserted_row_count       INT,  -- of loaded: new fact rows
    updated_row_count        INT,  -- of loaded: existing fact rows refreshed (re-runs)
    collapsed_row_count      INT,  -- usable - loaded: duplicate source rows merged
    missing_size_count       INT,  -- loaded rows with no weight and no pack count
    invalid_window_count     INT,  -- usable rows with valid_to < valid_from
    -- Snapshots so the NEXT batch can compare against this one even after its
    -- fact rows have been re-pointed to a newer batch by an upsert.
    category_counts          JSONB,
    chain_counts             JSONB,
    min_flyer_week           DATE,
    max_flyer_week           DATE,
    error                    TEXT,
    notes                    TEXT
);

CREATE INDEX IF NOT EXISTS idx_etl_batch_postal_recent
    ON etl_batch (postal_code, finished_at DESC);

-- ---------------------------------------------------------------------------
-- fact_price_observation: one row per product x store-market x flyer week
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_price_observation (
    observation_key   BIGSERIAL PRIMARY KEY,
    product_key       INT  NOT NULL REFERENCES dim_product (product_key),
    store_key         INT  NOT NULL REFERENCES dim_store (store_key),
    flyer_week_start  DATE NOT NULL REFERENCES dim_date (date_key),
    valid_from_date   DATE NOT NULL REFERENCES dim_date (date_key),
    valid_to_date     DATE          REFERENCES dim_date (date_key),
    valid_from        TIMESTAMPTZ NOT NULL,
    valid_to          TIMESTAMPTZ,
    -- When the flyer lists the same product several times in one week, the
    -- rows collapse into this one: price = lowest, price_max = highest.
    price             NUMERIC(10,2) NOT NULL CHECK (price > 0),
    price_max         NUMERIC(10,2) NOT NULL,
    is_ambiguous      BOOLEAN GENERATED ALWAYS AS (price_max <> price) STORED,
    unit_price        NUMERIC(10,2),
    unit_type         TEXT NOT NULL CHECK (unit_type IN ('kg', 'pack_unit', 'item')),
    price_per_kg      NUMERIC(10,2),
    price_per_unit    NUMERIC(10,2),
    weight_kg         NUMERIC(10,3),
    original_name     TEXT NOT NULL,
    flyer_id          BIGINT,
    source_row_count  INT NOT NULL DEFAULT 1 CHECK (source_row_count >= 1),
    first_batch_id    BIGINT NOT NULL REFERENCES etl_batch (batch_id),  -- never changes
    last_batch_id     BIGINT NOT NULL REFERENCES etl_batch (batch_id),  -- latest load that saw it
    CONSTRAINT uq_fact_grain UNIQUE (product_key, store_key, flyer_week_start),
    CONSTRAINT ck_price_range CHECK (price_max >= price)
);

COMMENT ON COLUMN fact_price_observation.is_ambiguous IS
    'True when duplicate flyer rows for this product-week had different prices '
    '(likely different variants under one name). Excluded from week-over-week math.';

-- uq_fact_grain's index (product_key, store_key, flyer_week_start) already
-- serves per-product price history, so no separate product index is needed.
CREATE INDEX IF NOT EXISTS idx_fact_store_week
    ON fact_price_observation (store_key, flyer_week_start);
CREATE INDEX IF NOT EXISTS idx_fact_week
    ON fact_price_observation (flyer_week_start);
CREATE INDEX IF NOT EXISTS idx_fact_last_batch
    ON fact_price_observation (last_batch_id);

-- ---------------------------------------------------------------------------
-- dq_check_results: one row per check per batch
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dq_check_results (
    dq_result_id  BIGSERIAL PRIMARY KEY,
    batch_id      BIGINT  NOT NULL REFERENCES etl_batch (batch_id),
    check_name    TEXT    NOT NULL,
    metric_value  NUMERIC,
    threshold     NUMERIC,
    comparison    TEXT    NOT NULL,  -- pass when: metric_value <comparison> threshold
    passed        BOOLEAN NOT NULL,
    severity      TEXT    NOT NULL DEFAULT 'warning',
    details       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_dq_batch_check UNIQUE (batch_id, check_name)
);
