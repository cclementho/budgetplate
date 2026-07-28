"""Flipp flyer scraper, integrated from https://github.com/Kiizon/flippscrape.

The original project is a CLI that writes a CSV. Here it is adapted into an
importable module: ``scrape_postal_code(postal_code)`` returns a list of raw
item dicts with the columns the upstream tool produced:

    merchant, flyer_id, name, price, valid_from, valid_to

Only grocery flyers for the supported merchants (No Frills, FreshCo, Walmart,
Loblaws) are kept. No API key is required.
"""

import random

import requests

FLYERS_URL = (
    "https://flyers-ng.flippback.com/api/flipp/data"
    "?locale=en&postal_code={}&sid={}"
)
FLYER_ITEMS_URL = (
    "https://flyers-ng.flippback.com/api/flipp/flyers/{}/flyer_items"
    "?locale=en&sid={}"
)

# Stores the rest of the app understands (must match config.SUPPORTED_MERCHANTS).
GROCERY_STORES = {"No Frills", "FreshCo", "Walmart", "Loblaws"}

REQUEST_TIMEOUT = 20


def generate_sid() -> str:
    """Generate a 16-digit session id for the Flipp API."""
    return "".join(str(random.randint(0, 9)) for _ in range(16))


def get_flyers_by_postal_code(postal_code: str) -> dict:
    """Fetch the flyer index for a postal code."""
    sid = generate_sid()
    resp = requests.get(
        FLYERS_URL.format(postal_code, sid), timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def get_grocery_flyer_ids(postal_code: str) -> list[dict]:
    """Return grocery flyer ids for the supported merchants.

    Filters the flyer index to merchants we support that are tagged as
    "Groceries", so non-grocery flyers (electronics, pharmacy, etc.) are
    excluded.
    """
    data = get_flyers_by_postal_code(postal_code)
    if "flyers" not in data:
        return []

    grocery_flyers = []
    for flyer in data["flyers"]:
        merchant = flyer.get("merchant")
        categories = flyer.get("categories", [])
        if isinstance(categories, str):
            categories = [c.strip() for c in categories.split(",")]

        if merchant in GROCERY_STORES and "Groceries" in categories:
            grocery_flyers.append({"id": flyer["id"], "merchant": merchant})

    return grocery_flyers


def get_flyer_items(flyer_id: int) -> list[dict]:
    """Return raw item records for a single flyer."""
    sid = generate_sid()
    resp = requests.get(
        FLYER_ITEMS_URL.format(flyer_id, sid), timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def scrape_postal_code(postal_code: str) -> list[dict]:
    """Scrape all supported grocery flyer items for a postal code.

    Returns a list of dicts with keys: merchant, flyer_id, name, price,
    valid_from, valid_to. Network errors on an individual flyer are skipped so
    one bad flyer does not abort the whole run.
    """
    postal_code = postal_code.replace(" ", "").upper()
    grocery_flyers = get_grocery_flyer_ids(postal_code)

    rows: list[dict] = []
    for flyer in grocery_flyers:
        flyer_id = flyer["id"]
        merchant = flyer["merchant"]
        try:
            items = get_flyer_items(flyer_id)
        except requests.RequestException:
            continue

        for item in items:
            rows.append(
                {
                    "merchant": merchant,
                    "flyer_id": flyer_id,
                    "name": item.get("name", ""),
                    "price": item.get("price", ""),
                    "valid_from": item.get("valid_from", ""),
                    "valid_to": item.get("valid_to", ""),
                }
            )

    return rows
