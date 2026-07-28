"""Nearby stores endpoint."""

from fastapi import APIRouter, Query

from jobs import start_background_scrape
from location.stores import find_stores
from queries import get_deal_counts
from scraper.pipeline import has_fresh_data

router = APIRouter()


@router.get("/stores")
def stores(
    postal_code: str = Query(..., description="Canadian postal code, e.g. V7C4V9"),
) -> dict:
    """Return supported grocery stores near a postal code.

    Each store includes distance, walking time, a walkability band, the count
    of deals on sale this week, and a composite score (cheaper + closer ranks
    higher). If no supported stores are found, ``stores`` is empty so the UI can
    show a friendly message.

    On the first visit to a new postal code there is no deal data yet, so a
    background scrape is started and ``status: "loading"`` is returned
    immediately. The frontend polls /api/scrape/status and refetches once ready
    — this keeps the "Find Deals" flow from blocking for 30-60s.
    """
    postal_code = postal_code.replace(" ", "").upper()

    if not has_fresh_data(postal_code):
        start_background_scrape(postal_code)
        return {
            "status": "loading",
            "postal_code": postal_code,
            "stores": [],
            "count": 0,
            "message": (
                "Fetching deals for your area, this takes about 30 seconds."
            ),
        }

    deal_counts = get_deal_counts(postal_code)
    result = find_stores(postal_code, deal_counts=deal_counts)

    return {
        "status": "ready",
        "postal_code": postal_code,
        "stores": result["stores"],
        "count": len(result["stores"]),
        # Set when only unsupported grocery stores were found nearby.
        "note": result["note"],
    }
