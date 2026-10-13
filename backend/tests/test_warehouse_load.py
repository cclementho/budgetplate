"""Loading behaviour: batches, idempotency, duplicate collapsing, bad rows."""

import datetime as dt
from decimal import Decimal

import pytest

from scraper import warehouse
from scraper.warehouse import flyer_week_start, load_batch
from tests.helpers import WEEK_1, parsed, query, raw_row, scalar

PC = "M5S1Z9"


@pytest.mark.parametrize(
    "day, expected",
    [
        (dt.date(2026, 10, 8), dt.date(2026, 10, 8)),   # Thursday -> itself
        (dt.date(2026, 10, 12), dt.date(2026, 10, 8)),  # Monday   -> previous Thursday
        (dt.date(2026, 10, 14), dt.date(2026, 10, 8)),  # Wednesday (last day) -> same week
        (dt.date(2026, 10, 15), dt.date(2026, 10, 15)), # next Thursday starts a new week
        (dt.date(2026, 10, 7), dt.date(2026, 10, 1)),   # Wednesday before -> prior week
    ],
)
def test_flyer_week_starts_on_thursday(day, expected):
    assert flyer_week_start(day) == expected


def _basket():
    return parsed(
        raw_row("PORK BELLY, 1 kg", "6.99"),
        raw_row("BOK CHOY", "1.99"),
        raw_row("JASMINE RICE, 8 kg", "15.99"),
    )


def test_load_writes_batch_dimensions_and_facts(db):
    summary = load_batch(PC, _basket(), raw_row_count=4)

    assert summary["loaded_row_count"] == 3
    batch = query(db, "SELECT * FROM etl_batch")[0]
    # Loaded and closed. (DQ also warns here, since 1 of 4 scraped rows had no
    # price, which is 25% against a 20% threshold; test_dq.py covers that.)
    assert batch["status"] in ("success", "success_with_warnings")
    assert batch["finished_at"] is not None
    assert batch["raw_row_count"] == 4 and batch["priced_row_count"] == 3
    assert batch["min_flyer_week"] == batch["max_flyer_week"] == WEEK_1
    assert scalar(db, "SELECT count(*) FROM dim_store") == 1
    assert scalar(db, "SELECT count(*) FROM dim_product") == 3

    pork = query(db, """
        SELECT f.* FROM fact_price_observation f
        JOIN dim_product p USING (product_key) WHERE p.normalized_name = 'pork belly'
    """)[0]
    assert pork["unit_type"] == "kg" and pork["unit_price"] == Decimal("6.99")
    assert pork["flyer_week_start"] == WEEK_1


def test_reloading_the_same_week_is_idempotent(db):
    first = load_batch(PC, _basket())
    second = load_batch(PC, _basket())

    # Same facts, not duplicates...
    assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 3
    assert (first["inserted_row_count"], first["updated_row_count"]) == (3, 0)
    assert (second["inserted_row_count"], second["updated_row_count"]) == (0, 3)
    # ...but both attempts are in the audit log.
    assert scalar(db, "SELECT count(*) FROM etl_batch") == 2
    # Lineage: first sighting is preserved, last sighting moves forward.
    lineage = query(db, "SELECT DISTINCT first_batch_id, last_batch_id FROM fact_price_observation")
    assert lineage == [{"first_batch_id": first["batch_id"], "last_batch_id": second["batch_id"]}]


def test_reload_picks_up_price_changes_in_place(db):
    load_batch(PC, parsed(raw_row("BOK CHOY", "1.99")))
    load_batch(PC, parsed(raw_row("BOK CHOY", "1.49")))  # corrected flyer, same week
    rows = query(db, "SELECT price FROM fact_price_observation")
    assert rows == [{"price": Decimal("1.49")}]


def test_duplicate_listings_collapse_into_one_ambiguous_fact(db):
    # Real Walmart flyer: "Halloween plush" listed 3x at $8.48 and 2x at $26.98.
    rows = parsed(*[raw_row("Halloween plush", "8.48", merchant="Walmart")] * 3,
                  *[raw_row("Halloween plush", "26.98", merchant="Walmart")] * 2)
    summary = load_batch(PC, rows)

    fact = query(db, "SELECT * FROM fact_price_observation")
    assert len(fact) == 1
    fact = fact[0]
    assert fact["price"] == Decimal("8.48")
    assert fact["price_max"] == Decimal("26.98")
    assert fact["source_row_count"] == 5
    assert fact["is_ambiguous"] is True
    assert summary["collapsed_row_count"] == 4


def test_identical_duplicates_are_not_ambiguous(db):
    load_batch(PC, parsed(*[raw_row("BOK CHOY", "1.99")] * 3))
    fact = query(db, "SELECT price, price_max, is_ambiguous, source_row_count FROM fact_price_observation")
    assert fact == [{"price": Decimal("1.99"), "price_max": Decimal("1.99"),
                     "is_ambiguous": False, "source_row_count": 3}]


def test_unusable_rows_are_dropped_and_counted(db):
    rows = parsed(
        raw_row("BOK CHOY", "1.99"),
        raw_row("GIFT CARD", "0"),                    # zero price
        raw_row("MYSTERY ITEM", "2.50", valid_from=""),  # no flyer week
    )
    summary = load_batch(PC, rows)
    assert summary["loaded_row_count"] == 1
    assert summary["dropped_row_count"] == 2
    assert summary["nonpositive_price_count"] == 1


def test_inverted_validity_window_is_kept_but_counted(db):
    # Seen in real data: valid_from 2026-06-25, valid_to 2026-06-22.
    rows = parsed(raw_row("BOK CHOY", "1.99",
                          valid_from="2026-06-25T00:00:00-04:00",
                          valid_to="2026-06-22T23:59:59-04:00"))
    summary = load_batch(PC, rows)
    assert summary["loaded_row_count"] == 1
    assert summary["invalid_window_count"] == 1


def test_failed_load_leaves_a_failed_batch_and_no_facts(db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(warehouse, "_upsert_facts", boom)
    with pytest.raises(RuntimeError):
        load_batch(PC, _basket())

    batch = query(db, "SELECT status, error FROM etl_batch")[0]
    assert batch["status"] == "failed"
    assert "database went away" in batch["error"]
    # The dimension upserts ran in the same transaction, so they rolled back too.
    assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 0
    assert scalar(db, "SELECT count(*) FROM dim_product") == 0
