-- BudgetPlate database schema (PostgreSQL).
-- Local-first, but the structure is Supabase-ready: a single flat table keyed
-- by postal_code with a scraped_at timestamp powers both caching and search.

CREATE TABLE IF NOT EXISTS flyer_items (
    id SERIAL PRIMARY KEY,
    postal_code VARCHAR(6),
    merchant VARCHAR(100),
    original_name TEXT,
    clean_name TEXT,
    category VARCHAR(50),
    price DECIMAL(10,2),
    price_per_kg DECIMAL(10,2),
    price_per_unit DECIMAL(10,2),
    weight_kg DECIMAL(10,3),
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,
    scraped_at TIMESTAMP DEFAULT NOW()
);

-- Common lookups: by postal code (cache + search) and recent-postal-code
-- discovery for the scheduled refresh job.
CREATE INDEX IF NOT EXISTS idx_flyer_items_postal ON flyer_items (postal_code);
CREATE INDEX IF NOT EXISTS idx_flyer_items_scraped_at ON flyer_items (scraped_at);
CREATE INDEX IF NOT EXISTS idx_flyer_items_merchant ON flyer_items (postal_code, merchant);
