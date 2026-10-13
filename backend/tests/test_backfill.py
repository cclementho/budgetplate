"""Backfill of flyer_items_legacy into the warehouse."""

import datetime as dt
from decimal import Decimal

import backfill
from tests.helpers import query, scalar


def _legacy(db, rows):
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO flyer_items_legacy (postal_code, merchant, original_name,
                    clean_name, category, price, valid_from, valid_to)
                VALUES (%(pc)s, %(merchant)s, %(orig)s, %(clean)s, %(cat)s,
                        %(price)s, %(vf)s, %(vt)s)
                """,
                r,
            )
    db.commit()


def _row(pc, orig, price, *, clean="Stale Name From Old Parser", cat="Other",
         vf="2026-07-02 03:00:00", vt="2026-07-08 23:59:59", merchant="No Frills"):
    return {"pc": pc, "merchant": merchant, "orig": orig, "clean": clean, "cat": cat,
            "price": price, "vf": vf, "vt": vt}


def test_backfill_reparses_names_instead_of_copying_them(db):
    _legacy(db, [_row("V7C4K2", "PORK BELLY, 1 kg", "6.99", clean="Pork")])
    backfill.run()

    product = query(db, "SELECT display_name, category, weight_kg FROM dim_product")[0]
    assert product == {"display_name": "Pork Belly", "category": "Meat & Seafood",
                       "weight_kg": Decimal("1.000")}


def test_naive_legacy_timestamps_are_read_as_eastern_time(db):
    # Flipp gave this BC flyer as 03:00-04:00 (midnight Pacific); the legacy
    # TIMESTAMP column kept only the wall clock, 03:00.
    _legacy(db, [_row("V7C4K2", "BOK CHOY", "1.99")])
    backfill.run()

    fact = query(db, "SELECT valid_from, flyer_week_start FROM fact_price_observation")[0]
    assert fact["valid_from"] == dt.datetime(2026, 7, 2, 7, 0, tzinfo=dt.timezone.utc)
    assert fact["flyer_week_start"] == dt.date(2026, 7, 2)


def test_one_backfill_batch_per_postal_code(db):
    _legacy(db, [_row("V7C4K2", "BOK CHOY", "1.99"),
                 _row("V7C4K2", "PORK BELLY", "6.99"),
                 _row("M5S1Z9", "BOK CHOY", "2.29", vf="2026-07-02 00:00:00")])
    results = backfill.run()

    assert sorted(r["postal_code"] for r in results) == ["M5S1Z9", "V7C4K2"]
    batches = query(db, "SELECT postal_code, source, triggered_by, loaded_row_count "
                        "FROM etl_batch ORDER BY postal_code")
    assert batches == [
        {"postal_code": "M5S1Z9", "source": "backfill", "triggered_by": "backfill",
         "loaded_row_count": 1},
        {"postal_code": "V7C4K2", "source": "backfill", "triggered_by": "backfill",
         "loaded_row_count": 2},
    ]


def test_rerunning_the_backfill_is_safe(db):
    _legacy(db, [_row("V7C4K2", "BOK CHOY", "1.99")])
    backfill.run()

    second = backfill.run()
    assert second[0]["status"] == "skipped"
    assert scalar(db, "SELECT count(*) FROM etl_batch") == 1

    forced = backfill.run(force=True)
    assert forced[0]["updated_row_count"] == 1 and forced[0]["inserted_row_count"] == 0
    assert scalar(db, "SELECT count(*) FROM fact_price_observation") == 1
