"""End-to-end data pipeline: scrape -> filter -> parse (pure Python) -> store.

Public entry point is ``run_pipeline(postal_code)``. It honours a 24h cache:
if fresh data already exists for the postal code, it is returned instead of
re-scraping.

No AI is involved here — parsing is regex/keyword-based and instant, so a full
scrape completes in seconds (network time only). Claude is only called later,
in /api/budget-plan, when building a user's weekly basket.
"""

from datetime import datetime, timedelta

import progress
from config import settings
from database import get_cursor
from scraper.flipp import scrape_postal_code
from scraper.parser import parse_items


def _normalize_postal(postal_code: str) -> str:
    return postal_code.replace(" ", "").upper()


def has_fresh_data(postal_code: str, hours: int | None = None) -> bool:
    """True if this postal code was scraped within the cache window."""
    hours = hours or settings.CACHE_HOURS
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with get_cursor() as cur:
        cur.execute(
            "SELECT MAX(scraped_at) AS last FROM flyer_items WHERE postal_code = %s",
            (_normalize_postal(postal_code),),
        )
        row = cur.fetchone()
    return bool(row and row["last"] and row["last"] >= cutoff)


def _store_items(postal_code: str, items: list[dict]) -> int:
    """Replace any existing rows for the postal code with freshly cleaned ones."""
    postal_code = _normalize_postal(postal_code)
    with get_cursor(commit=True) as cur:
        # A scrape is a full refresh for the postal code; clear stale rows first.
        cur.execute("DELETE FROM flyer_items WHERE postal_code = %s", (postal_code,))
        for it in items:
            cur.execute(
                """
                INSERT INTO flyer_items (
                    postal_code, merchant, original_name, clean_name, category,
                    price, price_per_kg, price_per_unit, weight_kg,
                    valid_from, valid_to, scraped_at
                ) VALUES (
                    %(postal_code)s, %(merchant)s, %(original_name)s, %(clean_name)s,
                    %(category)s, %(price)s, %(price_per_kg)s, %(price_per_unit)s,
                    %(weight_kg)s, %(valid_from)s, %(valid_to)s, NOW()
                )
                """,
                {"postal_code": postal_code, **it},
            )
    return len(items)


def run_pipeline(postal_code: str, force: bool = False) -> dict:
    """Run the full pipeline for a postal code.

    Steps:
      1. Return cached data if it is < 24h old (unless ``force``).
      2. Scrape Flipp for supported grocery flyers.
      3. Drop items with no price.
      4. Parse with the pure-Python parser (regex weights, keyword categories).
      5. Store the cleaned rows in PostgreSQL.

    Returns a status dict describing what happened.
    """
    postal_code = _normalize_postal(postal_code)

    if not force and has_fresh_data(postal_code):
        return {
            "postal_code": postal_code,
            "status": "cached",
            "message": "Fresh data already exists (scraped < 24h ago).",
        }

    raw = scrape_postal_code(postal_code)
    if not raw:
        return {
            "postal_code": postal_code,
            "status": "empty",
            "message": "No supported grocery flyers found for this postal code.",
            "item_count": 0,
        }

    # Filter out items with no usable price before spending tokens on them.
    priced = [r for r in raw if str(r.get("price", "")).strip() not in ("", "None")]

    # Track cleaning progress so the status endpoint can report "X of Y items".
    progress.start(postal_code, len(priced))
    try:
        cleaned = parse_items(
            priced, progress_cb=lambda n: progress.update(postal_code, n)
        )
        count = _store_items(postal_code, cleaned)
    finally:
        progress.clear(postal_code)

    return {
        "postal_code": postal_code,
        "status": "scraped",
        "message": f"Scraped and cleaned {count} items.",
        "item_count": count,
    }


def recent_postal_codes(days: int = 30) -> list[str]:
    """Postal codes searched within the last ``days`` (for the refresh job)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT postal_code FROM flyer_items WHERE scraped_at >= %s",
            (cutoff,),
        )
        rows = cur.fetchall()
    return [r["postal_code"] for r in rows]
