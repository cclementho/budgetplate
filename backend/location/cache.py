"""Postgres cache for nearby-store lookups (geocode + store list per postal code).

Stores near a postal code basically never change, while Nominatim and Overpass
are slow and rate-limited, so a lookup is reused for 30 days. Both functions
swallow database errors: a broken cache must only cost a slower request, never
a failed one.
"""

import json
import logging

from database import get_cursor

logger = logging.getLogger("budgetplate.stores")

CACHE_DAYS = 30


def _normalize(postal_code: str) -> str:
    return (postal_code or "").replace(" ", "").upper()


def get_cached_lookup(postal_code: str) -> dict | None:
    """Return ``{"lat", "lng", "entries"}`` if cached within CACHE_DAYS, else None."""
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT lat, lng, stores
                FROM store_lookup_cache
                WHERE postal_code = %s
                  AND fetched_at >= now() - make_interval(days => %s)
                """,
                (_normalize(postal_code), CACHE_DAYS),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("Store cache read failed for %s", postal_code)
        return None
    if not row:
        return None
    return {"lat": row["lat"], "lng": row["lng"], "entries": row["stores"]}


def save_lookup(postal_code: str, lat: float, lng: float, entries: list[dict]) -> None:
    """Store (or refresh) the lookup for a postal code."""
    try:
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO store_lookup_cache (postal_code, lat, lng, stores)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (postal_code) DO UPDATE SET
                    lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                    stores = EXCLUDED.stores, fetched_at = now()
                """,
                (_normalize(postal_code), lat, lng, json.dumps(entries)),
            )
    except Exception:
        logger.exception("Store cache write failed for %s", postal_code)
