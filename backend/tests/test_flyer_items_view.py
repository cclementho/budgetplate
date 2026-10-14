"""The flyer_items view keeps the live API working on top of the warehouse.

These tests drive the UNCHANGED queries.py functions the routes call, so they
prove the API contract survived the move from a table to a view.
"""

import queries
from scraper import pipeline
from scraper.pipeline import has_fresh_data, run_pipeline
from scraper.warehouse import load_batch
from tests.helpers import LAST_MONTH_WEEK, current_row, parsed, query, raw_row, scalar

PC = "M5S1Z9"

# Exactly what queries.get_items() selects.
API_COLUMNS = ["id", "postal_code", "merchant", "original_name", "clean_name",
               "category", "price", "price_per_kg", "price_per_unit", "weight_kg",
               "valid_from", "valid_to", "scraped_at"]


def test_flyer_items_is_now_a_view_with_the_api_columns(db):
    kind = scalar(db, "SELECT table_type FROM information_schema.tables "
                      "WHERE table_name = 'flyer_items'")
    assert kind == "VIEW"
    cols = [r["column_name"] for r in query(db, """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'flyer_items' ORDER BY ordinal_position""")]
    assert cols == API_COLUMNS


def test_api_queries_read_the_latest_scrape():
    load_batch(PC, parsed(
        current_row("BOK CHOY", "1.99"),
        current_row("PORK BELLY, 1 kg", "6.99", merchant="FreshCo"),
        *[current_row("Halloween plush", "8.48", merchant="Walmart")] * 3,
    ))

    items = queries.get_items(PC)
    assert sorted(i["clean_name"] for i in items) == ["Bok Choy", "Halloween Plush", "Pork Belly"]
    assert queries.get_items(PC, merchant="FreshCo")[0]["clean_name"] == "Pork Belly"
    # The flyer's triple listing reaches the API once, not three times.
    assert queries.get_deal_counts(PC) == {"No Frills": 1, "FreshCo": 1, "Walmart": 1}
    meta = queries.get_postal_meta(PC)
    assert meta["item_count"] == 3 and meta["scraped_at"] is not None


def test_a_rescrape_replaces_what_the_api_sees_but_history_keeps_it(db):
    load_batch(PC, parsed(current_row("BOK CHOY", "1.99"), current_row("RAPINI", "2.49")))
    load_batch(PC, parsed(current_row("BOK CHOY", "1.79")))  # rapini dropped from the flyer

    assert [i["clean_name"] for i in queries.get_items(PC)] == ["Bok Choy"]
    # Same semantics as the old DELETE-and-reinsert table for the API...
    # ...but nothing was deleted: rapini's observation is still in the warehouse.
    assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 2


def test_backfilling_an_older_week_does_not_hide_current_deals():
    load_batch(PC, parsed(current_row("BOK CHOY", "1.99")))
    # Backfill of a month-old flyer, run AFTER today's scrape.
    load_batch(PC, parsed(raw_row("PORK BELLY", "6.99", week=LAST_MONTH_WEEK)),
               source="backfill", triggered_by="backfill")

    assert [i["clean_name"] for i in queries.get_items(PC)] == ["Bok Choy"]


def test_only_real_scrapes_count_as_fresh_cache():
    load_batch(PC, parsed(raw_row("PORK BELLY", "6.99", week=LAST_MONTH_WEEK)),
               source="backfill", triggered_by="backfill")
    assert has_fresh_data(PC) is False  # a backfill is not this week's flyer

    load_batch(PC, [], raw_row_count=0)  # Flipp returned nothing
    assert has_fresh_data(PC) is False  # don't cache an empty scrape for 24h

    load_batch(PC, parsed(current_row("BOK CHOY", "1.99")))
    assert has_fresh_data(PC) is True


def test_pipeline_scrapes_into_the_warehouse_and_honours_the_cache(db, monkeypatch):
    scraped = [current_row("BOK CHOY", "1.99"), current_row("PORK BELLY, 1 kg", "6.99"),
               {**current_row("GIFT CARD", ""), "price": ""}]  # no price: dropped upstream
    calls = []
    monkeypatch.setattr(pipeline, "scrape_postal_code",
                        lambda pc: calls.append(pc) or scraped)

    first = run_pipeline("m5s 1z9")
    assert first["status"] == "scraped" and first["item_count"] == 2
    batch = query(db, "SELECT source, triggered_by, raw_row_count, priced_row_count "
                      "FROM etl_batch")[0]
    assert batch == {"source": "scrape", "triggered_by": "on_demand",
                     "raw_row_count": 3, "priced_row_count": 2}

    assert run_pipeline(PC)["status"] == "cached"
    assert len(calls) == 1  # the cache short-circuited the second scrape

    forced = run_pipeline(PC, force=True, triggered_by="github_actions")
    assert forced["status"] == "scraped"
    assert scalar(db, "SELECT count(*) FROM etl_batch") == 2
    assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 2  # idempotent
