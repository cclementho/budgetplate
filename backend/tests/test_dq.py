"""Data-quality checks: pure rules first, then end-to-end through a real load."""

import pytest

from scraper import dq
from scraper.dq import evaluate, total_variation_distance
from scraper.warehouse import load_batch
from tests.helpers import WEEK_1, WEEK_2, parsed, query, raw_row

PC = "M5S1Z9"

ALL_CHECKS = {
    "row_count_vs_previous",
    "chain_coverage_vs_previous",
    "missing_or_zero_price_pct",
    "missing_weight_unit_pct",
    "category_distribution_shift",
    "duplicate_keys_in_batch",
    "invalid_validity_window",
}


def snapshot(**overrides):
    """A healthy etl_batch snapshot, shaped like the real October batches."""
    base = {
        "batch_id": 1,
        "postal_code": PC,
        "raw_row_count": 1000,
        "priced_row_count": 905,      # 9.5% unpriced, like real scrapes
        "nonpositive_price_count": 0,
        "usable_row_count": 905,
        "loaded_row_count": 780,      # 13.8% collapsed duplicates
        "collapsed_row_count": 125,
        "missing_size_count": 400,    # 51% without weight/pack
        "invalid_window_count": 0,
        "category_counts": {"Other": 400, "Produce": 120, "Dairy & Eggs": 100,
                            "Meat & Seafood": 80, "Snacks & Drinks": 80},
        "chain_counts": {"Walmart": 380, "Loblaws": 250, "FreshCo": 100, "No Frills": 50},
        "week_counts": {"2026-10-08": 780},
    }
    base.update(overrides)
    return base


def by_name(results):
    return {r.check_name: r for r in results}


# --- pure rules -------------------------------------------------------------

def test_healthy_batch_passes_every_check():
    results = evaluate(snapshot(batch_id=2), snapshot(batch_id=1))
    assert {r.check_name for r in results} == ALL_CHECKS
    assert all(r.passed for r in results), [r for r in results if not r.passed]


def test_first_batch_has_no_baseline_but_still_checks_itself():
    results = by_name(evaluate(snapshot(invalid_window_count=2), previous=None))
    for name in ("row_count_vs_previous", "chain_coverage_vs_previous",
                 "category_distribution_shift"):
        assert results[name].passed and results[name].details["baseline"] is None
    assert not results["invalid_validity_window"].passed  # self-contained checks still run


@pytest.mark.parametrize(
    "check, current",
    [
        ("row_count_vs_previous", snapshot(week_counts={"2026-10-15": 300})),
        ("chain_coverage_vs_previous",
         snapshot(chain_counts={"Walmart": 380, "FreshCo": 100, "No Frills": 50})),
        ("missing_or_zero_price_pct", snapshot(priced_row_count=600)),
        ("missing_or_zero_price_pct", snapshot(nonpositive_price_count=250)),
        ("missing_weight_unit_pct", snapshot(missing_size_count=760)),
        ("category_distribution_shift",
         snapshot(category_counts={"Other": 700, "Produce": 80})),
        ("duplicate_keys_in_batch", snapshot(usable_row_count=1560, collapsed_row_count=780)),
        ("invalid_validity_window", snapshot(invalid_window_count=1)),
    ],
)
def test_each_check_flags_its_own_failure(check, current):
    results = by_name(evaluate(current, snapshot(batch_id=1)))
    assert not results[check].passed
    # ...and only that check: the others are unaffected by the single fault.
    assert [n for n, r in results.items() if not r.passed] == [check]


def test_row_count_compares_dominant_weeks_not_batch_totals():
    # The real July V7C4K2 scrape held two flyer weeks (~1,060 rows each).
    # Comparing totals (2,120 -> 1,001) would be a false -53% alarm.
    july = snapshot(week_counts={"2026-06-25": 1058, "2026-07-02": 1060, "2026-05-28": 32})
    october = snapshot(week_counts={"2026-10-08": 1001})
    result = by_name(evaluate(october, july))["row_count_vs_previous"]
    assert result.passed
    assert result.details["previous_week"] == "2026-07-02"
    assert result.metric_value == pytest.approx(-5.57, abs=0.01)


def test_total_variation_distance():
    assert total_variation_distance({"a": 1}, {"a": 5}) == 0.0
    assert total_variation_distance({"a": 1}, {"b": 1}) == 1.0
    assert total_variation_distance({"a": 3, "b": 1}, {"a": 1, "b": 1}) == 0.25


# --- end to end through the loader --------------------------------------------

ITEMS = [("PORK BELLY, 1 kg", "6.99"), ("CHICKEN THIGHS, 1 kg", "7.49"),
         ("BOK CHOY", "1.99"), ("JASMINE RICE, 8 kg", "15.99"),
         ("LARGE EGGS, 12's", "3.49"), ("MILK 2%, 4 L", "6.29"),
         ("FROZEN PIZZA", "4.99"), ("CHOCOLATE BAR, 100 g", "1.99"),
         ("BREAD", "2.79"), ("BUTTER, 454 g", "5.49")]


def healthy_rows(week):
    return parsed(*[raw_row(n, p, merchant=m, week=week)
                    for m in ("No Frills", "FreshCo") for n, p in ITEMS])


def bad_rows(week):
    """What a broken scrape looks like: FreshCo's flyer missing, a flyer fetched
    three times over, mostly non-food filler, and one inverted date range."""
    rows = []
    for _ in range(3):
        rows += [raw_row("BOK CHOY", "1.99", week=week),
                 raw_row("PORK BELLY, 1 kg", "6.99", week=week)]
        rows += [raw_row(f"HALLOWEEN DECOR {c}", "4.00", week=week) for c in "ABCDEF"]
    rows.append(raw_row("CORN", "0.50", valid_from=f"{week}T00:00:00-04:00",
                        valid_to="2026-10-01T23:59:59-04:00"))
    return parsed(*rows)


def test_good_batch_after_good_batch_is_clean(db):
    load_batch(PC, healthy_rows(WEEK_1))
    second = load_batch(PC, healthy_rows(WEEK_2))
    assert second["status"] == "success"
    assert second["dq_failed_checks"] == []
    stored = query(db, "SELECT count(*) AS n, bool_and(passed) AS ok FROM dq_check_results "
                       "WHERE batch_id = %s", (second["batch_id"],))
    assert stored == [{"n": 7, "ok": True}]


def test_bad_batch_is_flagged_stored_and_still_loaded(db):
    load_batch(PC, healthy_rows(WEEK_1))
    priced = bad_rows(WEEK_2)
    # The scraper also returned 15 rows with no price, which the pipeline drops.
    bad = load_batch(PC, priced, raw_row_count=len(priced) + 15)

    assert bad["status"] == "success_with_warnings"
    assert set(bad["dq_failed_checks"]) == ALL_CHECKS

    stored = query(db, "SELECT check_name, passed, metric_value, details FROM dq_check_results "
                       "WHERE batch_id = %s", (bad["batch_id"],))
    assert {r["check_name"] for r in stored if not r["passed"]} == ALL_CHECKS
    chains = next(r for r in stored if r["check_name"] == "chain_coverage_vs_previous")
    assert chains["details"]["missing_chains"] == ["FreshCo"]
    dupes = next(r for r in stored if r["check_name"] == "duplicate_keys_in_batch")
    assert dupes["details"]["top_duplicates"][0]["rows"] == 3

    # Warnings, not rejection: the batch's data is in the warehouse.
    assert query(db, "SELECT status FROM etl_batch WHERE batch_id = %s",
                 (bad["batch_id"],)) == [{"status": "success_with_warnings"}]
    assert query(db, "SELECT count(*) AS n FROM fact_price_observation WHERE last_batch_id = %s",
                 (bad["batch_id"],)) == [{"n": 9}]


def test_a_crashing_check_never_fails_the_load(db, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise ZeroDivisionError("bug in a check")

    monkeypatch.setattr(dq, "evaluate", broken)
    result = load_batch(PC, healthy_rows(WEEK_1))

    assert result["status"] == "success"
    assert query(db, "SELECT status FROM etl_batch") == [{"status": "success"}]
    assert "DQ checks crashed" in caplog.text


def test_rerunning_checks_for_a_batch_is_idempotent(db):
    batch = load_batch(PC, healthy_rows(WEEK_1))["batch_id"]
    dq.run_checks(batch)
    dq.run_checks(batch)
    assert query(db, "SELECT count(*) AS n FROM dq_check_results") == [{"n": 7}]
