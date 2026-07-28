"""All-deals-at-a-store endpoint."""

from fastapi import APIRouter, Query

from queries import get_items
from scraper.pipeline import has_fresh_data, run_pipeline

router = APIRouter()


@router.get("/deals")
def deals(
    postal_code: str = Query(..., description="Canadian postal code, e.g. V7C4V9"),
    merchant: str = Query(..., description="Store name, e.g. 'No Frills'"),
) -> dict:
    """Return all currently-valid deals at a specific store.

    Used by budget mode to show the full deal list for the chosen store.
    """
    postal_code = postal_code.replace(" ", "").upper()

    if not has_fresh_data(postal_code):
        run_pipeline(postal_code)

    items = get_items(postal_code, merchant=merchant)
    return {
        "postal_code": postal_code,
        "merchant": merchant,
        "deals": items,
        "count": len(items),
    }
