"""Nearby grocery store discovery using OpenStreetMap (no API key needed).

Pipeline:
  1. Geocode the postal code to lat/lng via Nominatim.
  2. Query the Overpass API for grocery stores within 5km (10km fallback).
  3. Match store names against the supported chains (substring, case-insensitive).
  4. Compute walking distance/time and a composite score.
  5. If no supported chain is nearby, fall back to listing ALL grocery stores
     within 10km with a note that deal data is limited.

The composite score blends a per-merchant price index (lower = cheaper) with
distance, so a slightly pricier store that is much closer can outrank a cheap
store far away — the core BudgetPlate differentiator.
"""

import logging
import math

import requests

from config import settings

logger = logging.getLogger("budgetplate.stores")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Multiple Overpass mirrors — the public instance rate-limits (it returns
# HTTP 200 with an empty body + a "remark" rather than an error), so we rotate
# across mirrors until one returns data.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# FSA-level fallback geocoder (free, no key) for Canadian postal codes that
# Nominatim/OSM doesn't have — its Canadian postal coverage is patchy.
ZIPPOPOTAM_URL = "https://api.zippopotam.us/CA/{}"

# Be a good OSM citizen: identify the app per their usage policy.
USER_AGENT = "BudgetPlate/1.0 (grocery price comparison; contact: budgetplate.app)"

WALK_SPEED_KMH = 5.0  # assumed walking speed
RADIUS_PRIMARY_M = 5000  # 5km
RADIUS_FALLBACK_M = 10000  # 10km fallback if the primary search is empty
REQUEST_TIMEOUT = 30

# Rough relative price index per supported chain (1.0 = cheapest baseline).
# No Frills / FreshCo are discount banners; Loblaws is a full-service banner.
PRICE_INDEX = {
    "No Frills": 1.0,
    "FreshCo": 1.0,
    "Walmart": 1.05,
    "Loblaws": 1.25,
}

# Substring keywords -> canonical supported merchant (checked case-insensitively
# against the store's name/brand/operator). Real Canadian Superstore is a Loblaw
# banner and shares Loblaws flyer data, so both keywords map to "Loblaws".
MERCHANT_KEYWORDS = [
    ("no frills", "No Frills"),
    ("nofrills", "No Frills"),
    ("freshco", "FreshCo"),
    ("walmart", "Walmart"),
    ("loblaws", "Loblaws"),
    ("loblaw", "Loblaws"),
    ("real canadian", "Loblaws"),
    ("superstore", "Loblaws"),
]

# OSM shop types we treat as "grocery stores" for the catch-all fallback list.
GROCERY_SHOP_TYPES = {"supermarket", "grocery"}

FALLBACK_NOTE = (
    "We found these stores near you but full deal data is only available for "
    "No Frills, FreshCo, Walmart, and Loblaws."
)


def _nominatim(params: dict) -> tuple[float, float] | None:
    """Run a Nominatim search (constrained to Canada) and return (lat, lng)."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={**params, "countrycodes": "ca", "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def _zippopotam_fsa(fsa: str) -> tuple[float, float] | None:
    """FSA-level (first 3 chars) centroid fallback for Canadian postal codes."""
    try:
        resp = requests.get(
            ZIPPOPOTAM_URL.format(fsa),
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        places = resp.json().get("places", [])
    except requests.RequestException:
        return None
    if not places:
        return None
    return float(places[0]["latitude"]), float(places[0]["longitude"])


def geocode_postal_code(postal_code: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a Canadian postal code, or None if not found.

    OSM/Nominatim's Canadian postal coverage is patchy (many valid postal codes
    and even FSAs are missing), so we try Nominatim first (accurate when it has
    the code, always constrained to Canada to avoid false matches abroad) and
    fall back to an FSA-level centroid — accurate enough for a 5-10km search.
    """
    pc = postal_code.replace(" ", "").upper()
    spaced = f"{pc[:3]} {pc[3:]}" if len(pc) >= 6 else pc
    fsa = pc[:3]

    # 1. Nominatim structured postalcode search — precise, and safely returns
    #    None rather than a fuzzy wrong match when OSM lacks the code. (Free-text
    #    search is deliberately NOT used: it fuzzy-matches postal codes to
    #    unrelated places, e.g. "V7C 4K2" -> a point in Alberta.)
    hit = _nominatim({"postalcode": spaced})
    if hit:
        return hit

    # 2. FSA centroid fallback for codes OSM doesn't have (e.g. much of BC).
    hit = _zippopotam_fsa(fsa)
    if hit:
        logger.info("Geocoded %s via FSA %s centroid fallback", postal_code, fsa)
        return hit

    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _canonical_merchant(tags: dict) -> str | None:
    """Resolve a store's tags to a supported merchant via substring matching.

    Matches if the store's name/brand/operator CONTAINS any supported keyword
    (case-insensitive), so "Real Canadian Superstore #123" resolves to Loblaws.
    """
    text = " ".join(
        [tags.get("brand", ""), tags.get("name", ""), tags.get("operator", "")]
    ).lower()
    for keyword, canonical in MERCHANT_KEYWORDS:
        if keyword in text:
            return canonical
    return None


def _distance_band(distance_km: float) -> str:
    """Classify travel difficulty for the UI distance badge."""
    if distance_km <= 1.0:
        return "walkable"
    if distance_km <= 3.0:
        return "short_transit"
    return "far"


def _format_address(tags: dict) -> str | None:
    """Build a street address string from OSM addr:* tags, if present."""
    house = tags.get("addr:housenumber")
    street = tags.get("addr:street")
    city = tags.get("addr:city")

    parts: list[str] = []
    if house and street:
        parts.append(f"{house} {street}")
    elif street:
        parts.append(street)
    if city:
        parts.append(city)
    return ", ".join(parts) if parts else None


def _query_overpass(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Fetch supermarkets, grocery, and convenience stores within a radius.

    Uses ``nwr`` so nodes, ways, and relations are all covered, and rotates
    across mirrors: a rate-limited mirror returns HTTP 200 with an empty body
    and a ``remark``, so we move on to the next one rather than treating that
    as "no stores".
    """
    query = f"""
    [out:json][timeout:25];
    (
      nwr["shop"="supermarket"](around:{radius_m},{lat},{lng});
      nwr["shop"="grocery"](around:{radius_m},{lat},{lng});
      nwr["shop"="convenience"](around:{radius_m},{lat},{lng});
    );
    out center tags;
    """
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            logger.info("Overpass endpoint failed (%s); trying next", endpoint)
            continue

        elements = payload.get("elements", [])
        if elements:
            return elements
        # Empty body with a remark => rate limit / timeout, not "no results".
        if payload.get("remark"):
            logger.info(
                "Overpass remark from %s: %s; trying next mirror",
                endpoint,
                payload["remark"],
            )
            continue
        return []  # genuine empty result

    logger.info("All Overpass mirrors failed/empty for radius %dm", radius_m)
    return []


def _parse_elements(
    elements: list[dict], origin_lat: float, origin_lng: float
) -> list[dict]:
    """Turn raw Overpass elements into {name, shop, merchant, coords, distance}."""
    parsed: list[dict] = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("brand") or tags.get("operator")
        if not name:
            continue

        if "lat" in el and "lon" in el:
            slat, slng = el["lat"], el["lon"]
        elif "center" in el:
            slat, slng = el["center"].get("lat"), el["center"].get("lon")
        else:
            continue
        if slat is None or slng is None:
            continue

        parsed.append(
            {
                "name": name,
                "shop": tags.get("shop"),
                "merchant": _canonical_merchant(tags),
                "address": _format_address(tags),
                "lat": slat,
                "lng": slng,
                "distance_km": round(_haversine_km(origin_lat, origin_lng, slat, slng), 2),
            }
        )
    return parsed


def _supported_store(entry: dict, deal_counts: dict) -> dict:
    """Build a store card for a supported chain."""
    distance_km = entry["distance_km"]
    merchant = entry["merchant"]
    price_index = PRICE_INDEX.get(merchant, 1.1)
    return {
        "name": entry["name"],
        "merchant": merchant,
        "address": entry.get("address"),
        "lat": entry["lat"],
        "lng": entry["lng"],
        "distance_km": distance_km,
        "walk_minutes": round((distance_km / WALK_SPEED_KMH) * 60),
        "band": _distance_band(distance_km),
        "price_index": price_index,
        "deals_count": deal_counts.get(merchant, 0),
        # Composite score: reward cheap prices and short distances. Higher is
        # better; distance dominates beyond a couple of km, which is exactly the
        # "closer beats slightly cheaper" behaviour we want.
        "score": round(10.0 / price_index - distance_km, 3),
        "supported": True,
    }


def _fallback_store(entry: dict) -> dict:
    """Build a store card for an unsupported grocery store (no deal data)."""
    distance_km = entry["distance_km"]
    return {
        "name": entry["name"],
        "merchant": entry["name"],  # no canonical chain; show the display name
        "address": entry.get("address"),
        "lat": entry["lat"],
        "lng": entry["lng"],
        "distance_km": distance_km,
        "walk_minutes": round((distance_km / WALK_SPEED_KMH) * 60),
        "band": _distance_band(distance_km),
        "price_index": None,
        "deals_count": 0,
        "score": round(-distance_km, 3),  # closer is better
        "supported": False,
    }


def _match_supported(parsed: list[dict], deal_counts: dict) -> list[dict]:
    """Pick the closest location per supported chain, ranked by composite score."""
    best_by_merchant: dict[str, dict] = {}
    for entry in parsed:
        merchant = entry["merchant"]
        if not merchant:
            continue
        existing = best_by_merchant.get(merchant)
        if existing is None or entry["distance_km"] < existing["distance_km"]:
            best_by_merchant[merchant] = entry

    stores = [_supported_store(e, deal_counts) for e in best_by_merchant.values()]
    stores.sort(key=lambda s: s["score"], reverse=True)
    return stores


def _search_radius(
    lat: float, lng: float, radius_m: int, postal_code: str
) -> list[dict]:
    """Query Overpass at a radius and log the store names it returns."""
    parsed = _parse_elements(_query_overpass(lat, lng, radius_m), lat, lng)
    logger.info(
        "Overpass returned %d stores within %dm for %s: %s",
        len(parsed),
        radius_m,
        postal_code,
        [p["name"] for p in parsed],
    )
    return parsed


def find_stores(postal_code: str, deal_counts: dict | None = None) -> dict:
    """Find grocery stores near a postal code.

    Returns ``{"stores": [...], "note": str | None}``:
      - Searches 5km first, then widens to 10km if no supported chain is found
        (this also covers the case where the 5km query returns nothing).
      - If any supported chain is within range, ``stores`` holds those
        (ranked by composite score) and ``note`` is None.
      - Otherwise ``stores`` holds ALL grocery stores within 10km (ranked by
        distance, each flagged ``supported: False``) and ``note`` explains that
        deal data is limited to the four supported chains.

    Each store dict contains: name, merchant, lat, lng, distance_km,
    walk_minutes, band, price_index, deals_count, score, supported.
    """
    deal_counts = deal_counts or {}
    coords = geocode_postal_code(postal_code)
    if coords is None:
        logger.info("Could not geocode postal code %s", postal_code)
        return {"stores": [], "note": None}
    lat, lng = coords

    # 1. Primary 5km search.
    parsed = _search_radius(lat, lng, RADIUS_PRIMARY_M, postal_code)
    supported = _match_supported(parsed, deal_counts)

    # 2. Widen to 10km if no supported chain turned up within 5km (a supported
    #    store may sit in the 5-10km ring even when closer non-supported ones
    #    exist, so we can't gate this on the 5km result being empty).
    if not supported:
        logger.info(
            "No supported store within %dm of %s; widening to %dm",
            RADIUS_PRIMARY_M,
            postal_code,
            RADIUS_FALLBACK_M,
        )
        parsed = _search_radius(lat, lng, RADIUS_FALLBACK_M, postal_code)
        supported = _match_supported(parsed, deal_counts)

    if supported:
        logger.info(
            "Matched %d supported store(s) for %s: %s",
            len(supported),
            postal_code,
            [s["merchant"] for s in supported],
        )
        return {"stores": supported, "note": None}

    # 3. Fallback: no supported chain within 10km — list ALL grocery stores
    #    within 10km so the user still sees what's around them.
    logger.info(
        "No supported stores within %dm of %s; listing all grocery stores",
        RADIUS_FALLBACK_M,
        postal_code,
    )
    seen: set = set()
    fallback_stores: list[dict] = []
    for entry in parsed:
        if entry["shop"] not in GROCERY_SHOP_TYPES:
            continue  # skip convenience stores in the "grocery stores" list
        key = (entry["name"].lower(), round(entry["lat"], 4), round(entry["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        fallback_stores.append(_fallback_store(entry))

    fallback_stores.sort(key=lambda s: s["distance_km"])
    note = FALLBACK_NOTE if fallback_stores else None
    return {"stores": fallback_stores, "note": note}
