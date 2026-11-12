-- 007: cache nearby-store lookups per postal code.
--
-- /api/stores used to call Nominatim and Overpass on every request (up to
-- 30 s each, across three Overpass mirrors). Which stores are near a postal
-- code almost never changes, so the geocode and store list are kept here for
-- 30 days (the TTL is applied when reading; see location/cache.py). Deal
-- counts are NOT cached: they are read fresh on every request.

CREATE TABLE IF NOT EXISTS store_lookup_cache (
    postal_code VARCHAR(7) PRIMARY KEY,
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    stores      JSONB       NOT NULL,   -- nearby stores: name, merchant, address, lat, lng, distance_km
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE store_lookup_cache IS
    'Nominatim + Overpass result per postal code; rows older than 30 days are ignored and overwritten.';

-- Same lockdown as every other table (005): no access through Supabase's Data API.
ALTER TABLE store_lookup_cache ENABLE ROW LEVEL SECURITY;
