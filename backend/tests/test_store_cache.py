"""Nearby-store lookups are cached per postal code for 30 days; deal counts are not."""

import logging

import pytest

from location import cache, stores
from tests.helpers import query

PC = "N6G2J8"
HERE = (43.0096, -81.2737)


def element(name, lat, lon, shop="supermarket"):
    return {"lat": lat, "lon": lon, "tags": {"name": name, "shop": shop}}


@pytest.fixture
def osm(monkeypatch):
    """Fake Nominatim + Overpass; counts how often each is called."""
    calls = {"geocode": 0, "overpass": 0}
    world = {"coords": HERE, "elements": [
        element("FreshCo", 43.0120, -81.2700),
        element("Walmart Supercentre", 43.0300, -81.2900),
        element("Corner Mart", 43.0100, -81.2740, shop="convenience"),
    ]}

    def geocode(postal_code):
        calls["geocode"] += 1
        return world["coords"]

    def overpass(lat, lng, radius_m):
        calls["overpass"] += 1
        return world["elements"]

    monkeypatch.setattr(stores, "geocode_postal_code", geocode)
    monkeypatch.setattr(stores, "_query_overpass", overpass)
    return calls, world


def test_second_lookup_comes_from_the_cache(osm):
    calls, _ = osm
    first = stores.find_stores(PC)
    second = stores.find_stores(PC)

    assert calls == {"geocode": 1, "overpass": 1}
    assert first["timing"]["cache"] == "miss" and second["timing"]["cache"] == "hit"
    assert [s["merchant"] for s in second["stores"]] == [s["merchant"] for s in first["stores"]]
    assert {s["merchant"] for s in second["stores"]} == {"FreshCo", "Walmart"}
    assert second["stores"][0]["distance_km"] == first["stores"][0]["distance_km"]


def test_deal_counts_are_fresh_on_every_call_not_cached(osm, db):
    stores.find_stores(PC, deal_counts={"FreshCo": 427})
    again = stores.find_stores(PC, deal_counts={"FreshCo": 12, "Walmart": 850})
    counts = {s["merchant"]: s["deals_count"] for s in again["stores"]}
    assert counts == {"FreshCo": 12, "Walmart": 850}
    (row,) = query(db, "SELECT stores::text AS stores FROM store_lookup_cache")
    assert "deals_count" not in row["stores"] and "Corner Mart" not in row["stores"]


def test_cache_expires_after_30_days(osm, db):
    calls, _ = osm
    stores.find_stores(PC)
    with db.cursor() as cur:
        cur.execute("UPDATE store_lookup_cache SET fetched_at = now() - interval '29 days'")
    db.commit()
    assert stores.find_stores(PC)["timing"]["cache"] == "hit"

    with db.cursor() as cur:
        cur.execute("UPDATE store_lookup_cache SET fetched_at = now() - interval '31 days'")
    db.commit()
    assert stores.find_stores(PC)["timing"]["cache"] == "miss"
    assert calls["overpass"] == 2
    (row,) = query(db, "SELECT fetched_at > now() - interval '1 minute' AS refreshed FROM store_lookup_cache")
    assert row["refreshed"]


def test_postal_codes_are_cached_separately_and_spacing_does_not_matter(osm):
    calls, _ = osm
    stores.find_stores("N6G 2J8")
    stores.find_stores("n6g2j8")
    assert calls["overpass"] == 1
    stores.find_stores("M5S1Z9")
    assert calls["overpass"] == 2


def test_failed_or_empty_lookups_are_not_cached(osm, db):
    calls, world = osm
    world["elements"] = []                       # e.g. every Overpass mirror was down
    assert stores.find_stores(PC)["stores"] == []
    world["coords"] = None                       # geocoder down
    assert stores.find_stores(PC)["stores"] == []
    assert query(db, "SELECT 1 FROM store_lookup_cache") == []

    world["coords"], world["elements"] = HERE, [element("FreshCo", 43.012, -81.27)]
    assert [s["merchant"] for s in stores.find_stores(PC)["stores"]] == ["FreshCo"]


def test_fallback_list_of_unsupported_stores_is_cached_too(osm):
    calls, world = osm
    world["elements"] = [element("Farm Boy", 43.012, -81.27)]
    first = stores.find_stores(PC)
    assert calls["overpass"] == 2                # 5 km, then widened to 10 km
    second = stores.find_stores(PC)
    assert calls["overpass"] == 2
    assert second["note"] == stores.FALLBACK_NOTE == first["note"]
    assert [s["name"] for s in second["stores"]] == ["Farm Boy"]


def test_a_broken_cache_only_costs_a_slower_request(osm, monkeypatch, caplog):
    calls, _ = osm

    def boom(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(cache, "get_cursor", boom)
    with caplog.at_level(logging.ERROR, logger="budgetplate.stores"):
        result = stores.find_stores(PC)
    assert {s["merchant"] for s in result["stores"]} == {"FreshCo", "Walmart"}
    assert "Store cache read failed" in caplog.text and "Store cache write failed" in caplog.text


def test_geocoding_times_out_after_10_seconds_and_overpass_after_20():
    assert (stores.REQUEST_TIMEOUT, stores.OVERPASS_TIMEOUT) == (10, 20)


def test_overpass_outage_does_not_trigger_the_wider_search(osm, monkeypatch):
    calls, _ = osm

    def down(lat, lng, radius_m):
        calls["overpass"] += 1
        return None                      # every mirror failed

    monkeypatch.setattr(stores, "_query_overpass", down)
    assert stores.find_stores(PC)["stores"] == []
    assert calls["overpass"] == 1        # no second, 10 km attempt


def test_mirrors_are_asked_together_and_the_first_real_answer_wins(monkeypatch):
    import time

    stores_found = [{"lat": 43.0, "lon": -81.2, "tags": {"name": "FreshCo", "shop": "supermarket"}}]

    def ask(endpoint, query):
        if "overpass-api.de" in endpoint:
            return None                  # at capacity: fails at once
        if "kumi" in endpoint:
            time.sleep(1.5)              # hangs
            return stores_found
        time.sleep(0.2)
        return stores_found              # the healthy mirror

    monkeypatch.setattr(stores, "_ask_overpass", ask)
    started = time.perf_counter()
    assert stores._query_overpass(43.0, -81.2, 5000) == stores_found
    assert time.perf_counter() - started < 1.0     # didn't wait for the slow mirror

    monkeypatch.setattr(stores, "_ask_overpass", lambda e, q: None)
    assert stores._query_overpass(43.0, -81.2, 5000) is None
    monkeypatch.setattr(stores, "_ask_overpass", lambda e, q: [] if "mail" in e else None)
    assert stores._query_overpass(43.0, -81.2, 5000) == []    # a clean "no stores here"
