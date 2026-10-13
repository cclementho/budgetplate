"""End-to-end data pipeline: scrape -> filter -> parse (pure Python) -> warehouse.

Public entry point is ``run_pipeline(postal_code)``. It honours a 24h cache:
if fresh data already exists for the postal code, it is returned instead of
re-scraping. Every scrape is appended to the price-history warehouse as a new
batch (scraper/warehouse.py) and data-quality checked (scraper/dq.py); the API
reads the latest batch through the flyer_items view.

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
from scraper.warehouse import load_batch


def _normalize_postal(postal_code: str) -> str:
    return postal_code.replace(" ", "").upper()


def has_fresh_data(postal_code: str, hours: int | None = None) -> bool:
    """True if a scrape for this postal code loaded rows within the cache window.

    Backfill batches and empty scrapes don't count: neither means we hold
    this week's flyer, so they must not suppress a fresh scrape.
    """
    hours = hours or settings.CACHE_HOURS
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM etl_batch
                WHERE postal_code = %s
                  AND source = 'scrape'
                  AND status IN ('success', 'success_with_warnings')
                  AND loaded_row_count > 0
                  AND finished_at >= now() - make_interval(hours => %s)
            ) AS fresh
            """,
            (_normalize_postal(postal_code), hours),
        )
        return cur.fetchone()["fresh"]


def run_pipeline(
    postal_code: str, force: bool = False, triggered_by: str = "on_demand"
) -> dict:
    """Run the full pipeline for a postal code.

    Steps:
      1. Return cached data if it is < 24h old (unless ``force``).
      2. Scrape Flipp for supported grocery flyers.
      3. Drop items with no price.
      4. Parse with the pure-Python parser (regex weights, keyword categories).
      5. Append the rows to the warehouse as a new batch, then run DQ checks.

    ``triggered_by`` is recorded on the batch: on_demand (a user request),
    scheduled (local APScheduler), github_actions (prod weekly job) or manual.

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

    # Filter out items with no usable price; the DQ check still sees how many
    # there were via raw_row_count.
    priced = [r for r in raw if str(r.get("price", "")).strip() not in ("", "None")]

    # Track progress so the status endpoint can report "X of Y items".
    progress.start(postal_code, len(priced))
    try:
        cleaned = parse_items(
            priced, progress_cb=lambda n: progress.update(postal_code, n)
        )
        # Empty scrapes are recorded too: a scrape that returned nothing is
        # exactly what the row-count DQ check should catch.
        batch = load_batch(
            postal_code,
            cleaned,
            raw_row_count=len(raw),
            source="scrape",
            triggered_by=triggered_by,
        )
    finally:
        progress.clear(postal_code)

    count = batch["loaded_row_count"]
    if not raw:
        status, message = "empty", "No supported grocery flyers found for this postal code."
    else:
        status, message = "scraped", f"Scraped and cleaned {count} items."
    return {
        "postal_code": postal_code,
        "status": status,
        "message": message,
        "item_count": count,
        "batch_id": batch["batch_id"],
        "batch_status": batch["status"],
        "dq_failed_checks": batch["dq_failed_checks"],
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
