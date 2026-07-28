"""Search-mode endpoint: fuzzy item search ranked by price-per-kg and distance."""

from fastapi import APIRouter, Query

from ai.search import search_items
from jobs import start_background_scrape
from location.stores import find_stores
from queries import get_items
from scraper.pipeline import has_fresh_data

router = APIRouter()


def _store_lookup(postal_code: str) -> dict:
    """Map merchant -> {distance_km, walk_minutes, band} for the postal code."""
    try:
        stores = find_stores(postal_code)["stores"]
    except Exception:
        stores = []
    return {
        s["merchant"]: {
            "distance_km": s["distance_km"],
            "walk_minutes": s["walk_minutes"],
            "band": s["band"],
        }
        for s in stores
    }


def _enrich(item: dict, stores: dict) -> dict:
    """Attach store distance info to an item row."""
    store = stores.get(item["merchant"], {})
    return {
        **item,
        "distance_km": store.get("distance_km"),
        "walk_minutes": store.get("walk_minutes"),
        "band": store.get("band"),
    }


@router.get("/items")
def search(
    postal_code: str = Query(..., description="Canadian postal code, e.g. V7C4V9"),
    query: str = Query(..., description="Search text, e.g. 'chicken breast'"),
) -> dict:
    """Search items for a postal code and rank by relevance.

    Returns the top matches (with a Claude-assigned similarity score), each
    enriched with store distance/walking time, a "best value" flag on the
    cheapest per-kg match within 2km, and a list of substitute products.
    """
    postal_code = postal_code.replace(" ", "").upper()

    # First search for a postal code has no cached data. Scraping+cleaning takes
    # 30-60s, so run it on a background thread and return a loading state
    # immediately. The frontend polls /api/scrape/status and re-searches once
    # the data is ready. Subsequent searches hit the database instantly.
    if not has_fresh_data(postal_code):
        start_background_scrape(postal_code)
        return {
            "status": "loading",
            "message": (
                "Fetching deals for your area, this takes about 30 seconds. "
                "Please search again in a moment."
            ),
            "postal_code": postal_code,
            "results": [],
            "similar": [],
        }

    catalogue = get_items(postal_code)
    if not catalogue:
        return {
            "status": "empty",
            "query": query,
            "postal_code": postal_code,
            "results": [],
            "similar": [],
            "message": "No deals found for this postal code.",
        }

    ranked = search_items(query, catalogue)
    by_id = {it["id"]: it for it in catalogue}
    stores = _store_lookup(postal_code)

    # Build the match list, preserving the model's ranking.
    results = []
    for m in ranked.get("matches", []):
        item = by_id.get(m.get("id"))
        if not item:
            continue
        enriched = _enrich(item, stores)
        enriched["similarity"] = m.get("similarity")
        results.append(enriched)

    # "Best value": lowest price_per_kg among matches within 2km.
    best_value_id = None
    best_ppk = None
    for r in results:
        ppk = r.get("price_per_kg")
        dist = r.get("distance_km")
        if ppk is not None and dist is not None and dist <= 2.0:
            if best_ppk is None or ppk < best_ppk:
                best_ppk = ppk
                best_value_id = r["id"]
    for r in results:
        r["best_value"] = r["id"] == best_value_id

    # If the top match is sold per unit (no per-kg), flag it for the UI.
    sold_per_unit = bool(
        results and results[0].get("price_per_kg") is None
    )

    similar = []
    for s in ranked.get("similar", []):
        item = by_id.get(s.get("id"))
        if not item:
            continue
        enriched = _enrich(item, stores)
        enriched["reason"] = s.get("reason")
        similar.append(enriched)

    return {
        "status": "ready",
        "query": query,
        "postal_code": postal_code,
        "results": results,
        "similar": similar,
        "sold_per_unit": sold_per_unit,
    }
