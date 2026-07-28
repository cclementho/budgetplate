"""Scrape trigger + status endpoints."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, field_validator

import progress
from jobs import is_scraping
from queries import get_postal_meta
from scraper.pipeline import has_fresh_data, run_pipeline

router = APIRouter()


class ScrapeRequest(BaseModel):
    postal_code: str
    force: bool = False

    @field_validator("postal_code")
    @classmethod
    def clean_postal(cls, v: str) -> str:
        return v.replace(" ", "").upper()


@router.post("/scrape")
def scrape(req: ScrapeRequest) -> dict:
    """Trigger the scrape+clean pipeline for a postal code.

    Honours the 24h cache unless ``force`` is true. Returns a job-status dict
    describing whether data was scraped, served from cache, or empty.
    """
    return run_pipeline(req.postal_code, force=req.force)


@router.get("/scrape/status")
def scrape_status(
    postal_code: str = Query(..., description="Canadian postal code, e.g. V7C4V9"),
) -> dict:
    """Report scrape progress for a postal code.

    Used by the frontend to poll after a search returns ``loading``:
      - ``ready``   : fresh data is available (poll can stop, re-run search)
      - ``loading`` : a background scrape is still running (keep polling)
      - ``empty``   : scrape finished but no deals were found for this area

    While a scrape is running, the response also carries ``processed`` and
    ``total`` item counts for a progress indicator.
    """
    postal_code = postal_code.replace(" ", "").upper()
    meta = get_postal_meta(postal_code)

    if is_scraping(postal_code):
        status = "loading"
    elif meta["item_count"] > 0 and has_fresh_data(postal_code):
        status = "ready"
    elif meta["item_count"] > 0:
        # Stale data exists but nothing is refreshing it — still usable.
        status = "ready"
    else:
        status = "empty"

    response = {
        "status": status,
        "postal_code": postal_code,
        "item_count": meta["item_count"],
        "scraped_at": meta["scraped_at"],
    }

    # While cleaning is underway, include progress so the UI can show
    # "Processing X of Y items…" instead of a bare spinner.
    prog = progress.get(postal_code)
    if prog is not None:
        response["processed"] = prog["processed"]
        response["total"] = prog["total"]

    return response
