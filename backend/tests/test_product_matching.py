"""Matching the same product across weeks: chain + normalized name + size."""

from decimal import Decimal

from scraper.warehouse import load_batch, normalize_name
from tests.helpers import WEEK_1, WEEK_2, WEEK_3, parsed, query, raw_row, scalar

PC = "M5S1Z9"


def test_normalize_name_ignores_case_punctuation_and_spacing():
    assert normalize_name("Pork Belly") == "pork belly"
    assert normalize_name("  PORK  BELLY!! ") == "pork belly"
    assert normalize_name("Liberté Greek-Yogurt") == "liberté greek yogurt"


def test_same_product_across_weeks_shares_one_product_key(db):
    load_batch(PC, parsed(raw_row("PORK BELLY, 1 kg", "6.99", week=WEEK_1)))
    load_batch(PC, parsed(raw_row("PORK BELLY, 1 kg", "5.49", week=WEEK_2)))

    assert scalar(db, "SELECT count(*) FROM dim_product") == 1
    history = query(db, """
        SELECT flyer_week_start, price FROM fact_price_observation ORDER BY flyer_week_start
    """)
    assert history == [
        {"flyer_week_start": WEEK_1, "price": Decimal("6.99")},
        {"flyer_week_start": WEEK_2, "price": Decimal("5.49")},
    ]
    product = query(db, "SELECT first_seen_week, last_seen_week FROM dim_product")[0]
    assert (product["first_seen_week"], product["last_seen_week"]) == (WEEK_1, WEEK_2)


def test_case_and_punctuation_differences_still_match(db):
    load_batch(PC, parsed(raw_row("Pork Belly", "6.99", week=WEEK_1)))
    load_batch(PC, parsed(raw_row("PORK BELLY!", "5.49", week=WEEK_2)))
    assert scalar(db, "SELECT count(*) FROM dim_product") == 1


def test_different_size_is_a_different_product(db):
    load_batch(PC, parsed(raw_row("JASMINE RICE, 1 kg", "3.99"),
                          raw_row("JASMINE RICE, 8 kg", "15.99")))
    sizes = query(db, "SELECT weight_kg FROM dim_product ORDER BY weight_kg")
    assert sizes == [{"weight_kg": Decimal("1.000")}, {"weight_kg": Decimal("8.000")}]


def test_different_pack_count_is_a_different_product(db):
    load_batch(PC, parsed(raw_row("LARGE EGGS, 12's", "3.49"),
                          raw_row("LARGE EGGS, 30's", "7.99")))
    assert scalar(db, "SELECT count(*) FROM dim_product") == 2


def test_same_name_at_another_chain_is_a_different_product(db):
    load_batch(PC, parsed(raw_row("BOK CHOY", "1.99", merchant="No Frills"),
                          raw_row("BOK CHOY", "2.49", merchant="FreshCo")))
    assert scalar(db, "SELECT count(*) FROM dim_product") == 2
    # Cross-chain comparison still works through the shared normalized name.
    assert scalar(db, "SELECT count(DISTINCT normalized_name) FROM dim_product") == 1


def test_loading_an_older_week_does_not_overwrite_newer_attributes(db):
    load_batch(PC, parsed(raw_row("Pork Belly", "5.49", week=WEEK_3)))
    load_batch(PC, parsed(raw_row("PORK BELLY", "6.99", week=WEEK_1)))  # late backfill

    product = query(db, "SELECT display_name, first_seen_week, last_seen_week FROM dim_product")[0]
    assert product["display_name"] == "Pork Belly"  # from the newest week, WEEK_3
    assert (product["first_seen_week"], product["last_seen_week"]) == (WEEK_1, WEEK_3)


def test_known_limitation_renamed_product_does_not_match(db):
    """Documented limitation: a chain renaming an item breaks the match."""
    load_batch(PC, parsed(raw_row("PORK BELLY", "6.99", week=WEEK_1)))
    load_batch(PC, parsed(raw_row("FRESH PORK BELLY", "5.49", week=WEEK_2)))
    assert scalar(db, "SELECT count(*) FROM dim_product") == 2
