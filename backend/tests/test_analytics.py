"""Every query in analytics/queries.sql runs, and returns the right answers on a
small dataset whose results are known in advance."""

import os
import re
from decimal import Decimal

import pytest

from scraper.warehouse import load_batch
from tests.helpers import WEEK_1, WEEK_2, parsed, query, raw_row

SQL_FILE = os.path.join(os.path.dirname(__file__), "..", "analytics", "queries.sql")
PC = "M5S1Z9"


def load_queries() -> dict[str, str]:
    """Split queries.sql on its "-- name: <id>" markers."""
    text = open(SQL_FILE).read()
    parts = re.split(r"^-- name: (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


QUERIES = load_queries()

# 1 kg each, so unit price == shelf price. (With e.g. "10 lb", rounding
# $2.00/4.536 kg to cents turns a flat +10% into a fake 5% "discount".)
PRODUCE_KG = ["TOMATO, 1 kg", "POTATO, 1 kg", "CARROT, 1 kg", "BROCCOLI, 1 kg", "MANGO, 1 kg"]


@pytest.fixture
def history():
    """Two flyer weeks at one No Frills market."""
    week1 = [
        raw_row("CHICKEN THIGHS, 1 kg", "7.49", week=WEEK_1),
        raw_row("LARGE EGGS, 12's", "3.49", week=WEEK_1),
        raw_row("JASMINE RICE, 8 kg", "15.99", week=WEEK_1),
        raw_row("BEEF PRICE DROP, 1 kg", "9.99", week=WEEK_1),  # must not match "rice"
        *[raw_row(n, "2.00", week=WEEK_1) for n in PRODUCE_KG],
    ]
    week2 = [
        raw_row("CHICKEN THIGHS, 1 kg", "5.99", week=WEEK_2),   # -20% (a real deal)
        raw_row("LARGE EGGS, 12's", "3.49", week=WEEK_2),       # unchanged
        raw_row("JASMINE RICE, 8 kg", "15.99", week=WEEK_2),    # two prices the same
        raw_row("JASMINE RICE, 8 kg", "19.99", week=WEEK_2),    # week: ambiguous
        raw_row("BEEF PRICE DROP, 1 kg", "10.99", week=WEEK_2),  # +10%
        *[raw_row(n, "2.20", week=WEEK_2) for n in PRODUCE_KG],  # produce +10%
    ]
    load_batch(PC, parsed(*week1))
    load_batch(PC, parsed(*week2))


def run(db, name):
    return query(db, QUERIES[name])


def test_the_file_defines_the_four_queries():
    assert set(QUERIES) == {"q1_chain_discount_frequency", "q2_week_over_week_common_items",
                            "q3_share_of_real_deals", "q4_category_price_trends"}


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_every_query_runs_on_an_empty_warehouse(db, name):
    assert run(db, name) == []


def test_q2_week_over_week_excludes_ambiguous_rows_and_non_matches(db, history):
    rows = {r["display_name"]: r for r in run(db, "q2_week_over_week_common_items")}
    assert set(rows) == {"Chicken Thighs", "Large Eggs"}  # no ambiguous rice, no "price"
    assert rows["Chicken Thighs"]["pct_change"] == Decimal("-20.0")
    assert rows["Chicken Thighs"]["weeks_apart"] == 1
    assert rows["Large Eggs"]["pct_change"] == Decimal("0.0")


def test_q3_judges_each_week_against_earlier_weeks_only(db, history):
    weeks = {r["week"]: r for r in run(db, "q3_share_of_real_deals")}
    assert weeks[WEEK_1]["with_prior_history"] == 0  # nothing earlier to compare with
    w2 = weeks[WEEK_2]
    # chicken, eggs, beef and 5 produce items (the ambiguous rice is excluded)
    assert w2["advertised"] == 8 and w2["with_prior_history"] == 8
    assert (w2["below_usual"], w2["same_as_usual"], w2["above_usual"]) == (1, 1, 6)
    assert w2["pct_real_deals"] == Decimal("12.5")


def test_q1_counts_only_discounts_of_at_least_five_percent(db, history):
    (no_frills,) = run(db, "q1_chain_discount_frequency")
    assert no_frills["chain"] == "No Frills"
    # chicken (2 weeks), eggs (2), beef (2), produce (5 x 2) = 16 observations;
    # only chicken's week-2 price is >= 5% under its usual price.
    assert no_frills["observations_with_history"] == 16
    assert no_frills["discounted"] == 1


def test_q4_tracks_median_price_per_kg_by_category(db, history):
    produce = [r for r in run(db, "q4_category_price_trends") if r["category"] == "Produce"]
    assert [r["week"] for r in produce] == [WEEK_1, WEEK_2]
    assert produce[0]["pct_change"] is None and produce[1]["pct_change"] == Decimal("10.0")
