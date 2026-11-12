"""Flipp flyer scraper, integrated from https://github.com/Kiizon/flippscrape.

The original project is a CLI that writes a CSV. Here it is adapted into an
importable module: ``scrape_postal_code(postal_code)`` returns a list of raw
item dicts with the columns the upstream tool produced:

    merchant, flyer_id, name, price, valid_from, valid_to

plus ``item_id`` and, for items that may be sold by weight, ``price_text`` and
``description`` from Flipp's item detail endpoint. The flyer list itself has no
unit field: "$2.00/lb" arrives there as a bare price of 2.00. The unit is only
on the per-item detail, as ``price_text`` (e.g. "lb", "/lb") with the per-kg
price in ``description`` (e.g. "8.82/KG").

Only grocery flyers for the supported merchants (No Frills, FreshCo, Walmart,
Loblaws) are kept. No API key is required.
"""

import logging
import random
from concurrent.futures import ThreadPoolExecutor

import requests

from scraper.parser import may_be_sold_by_weight

logger = logging.getLogger("budgetplate.flipp")

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

FLYER_ITEM_URL = (
    "https://flyers-ng.flippback.com/api/flipp/flyer_items/{}"
    "?locale=en&sid={}"
)

REQUEST_TIMEOUT = 20

# One detail request per item would be thousands per scrape, so details are
# only fetched for items that may be sold by weight (about 100 per postal
# code, 1-2 seconds in total), a few at a time and never more than this many.
MAX_DETAIL_LOOKUPS = 300
DETAIL_WORKERS = 8
DETAIL_TIMEOUT = 10


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


def get_flyer_item_detail(item_id: int) -> dict:
    """Return Flipp's detail record for one flyer item ({} on any failure)."""
    try:
        resp = requests.get(
            FLYER_ITEM_URL.format(item_id, generate_sid()), timeout=DETAIL_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def add_price_units(rows: list[dict]) -> int:
    """Fill ``price_text`` and ``description`` on rows that may be sold by weight.

    Mutates ``rows`` in place and returns how many details were fetched. A
    failed lookup just leaves the row as it was (priced per item).
    """
    targets = [
        r for r in rows
        if r.get("item_id")
        and str(r.get("price", "")).strip() not in ("", "None")
        and may_be_sold_by_weight(r.get("name", ""))
    ][:MAX_DETAIL_LOOKUPS]
    if not targets:
        return 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        details = list(pool.map(lambda r: get_flyer_item_detail(r["item_id"]), targets))
    for row, detail in zip(targets, details):
        row["price_text"] = detail.get("price_text")
        row["description"] = detail.get("description")
    return len(targets)


def scrape_postal_code(postal_code: str) -> list[dict]:
    """Scrape all supported grocery flyer items for a postal code.

    Returns a list of dicts with keys: merchant, flyer_id, item_id, name,
    price, valid_from, valid_to (and price_text / description where looked up). Network errors on an individual flyer are skipped so
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
                    "item_id": item.get("id"),
                    "name": item.get("name", ""),
                    "price": item.get("price", ""),
                    "valid_from": item.get("valid_from", ""),
                    "valid_to": item.get("valid_to", ""),
                }
            )

    looked_up = add_price_units(rows)
    logger.info(
        "Flipp %s: %d items from %d flyers; %d price-unit lookups",
        postal_code, len(rows), len(grocery_flyers), looked_up,
    )
    return rows
