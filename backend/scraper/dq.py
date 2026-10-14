"""Data-quality checks run after every warehouse load.

Design: each check is a pure function of two batch snapshots, the batch just
loaded and the previous successful batch for the same postal code (both are
etl_batch rows). That keeps the rules unit-testable without a database.
``run_checks()`` is the impure wrapper: it reads the snapshots, evaluates,
upserts one dq_check_results row per check, and marks the batch
'success_with_warnings' if anything failed.

Failures are warnings, never errors. ``run_checks()`` does not raise: if a
check itself crashes, that is logged and the (already committed) load stands.

Thresholds were calibrated on real batches: live scrapes of M5S1Z9, V7C4K2
and M5V3L9 (Oct 2026) and the July legacy data. Observed ranges are noted
next to each one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from database import get_conn

logger = logging.getLogger("budgetplate.dq")

# Rows in the dominant flyer week may not fall by more than this vs the
# previous batch. Observed: -5% .. +5% between real weekly batches.
MIN_ROW_CHANGE_PCT = -40.0
# Chains present in the previous batch but missing now. Observed: Loblaws
# vanished from M5V3L9's Oct scrape; a whole chain's flyer failed to load.
MAX_MISSING_CHAINS = 0
# Scraped rows with no price, or a price <= 0. Observed: 9.3% .. 10.4%
# (gift cards, "see in store" items).
MAX_MISSING_PRICE_PCT = 20.0
# Loaded rows with neither a parsed weight nor a pack count. Observed: 43% .. 64%
# (produce and items sold each). A broken weight regex pushes this toward 100%.
MAX_MISSING_WEIGHT_UNIT_PCT = 75.0
# Total variation distance between category mixes (0 = identical, 1 = disjoint):
# the share of rows that would have to change category to make the mixes match.
# Observed: 0.035 .. 0.065.
MAX_CATEGORY_TVD = 0.15
# Source rows merged into another row with the same grain key. Observed:
# 11% .. 20%. A flyer fetched twice would put this near 50%.
MAX_DUPLICATE_PCT = 35.0
# Rows whose validity ends before it starts. Observed once in the July data.
MAX_INVALID_WINDOWS = 0


@dataclass
class CheckResult:
    check_name: str
    metric_value: float | None
    threshold: float
    comparison: str  # passes when: metric_value <comparison> threshold
    passed: bool
    details: dict = field(default_factory=dict)
    severity: str = "warning"


def _pct(part, whole) -> float | None:
    return None if not whole else round(100.0 * part / whole, 2)


def _dominant_week(week_counts: dict | None) -> tuple[str | None, int]:
    if not week_counts:
        return None, 0
    week = max(week_counts, key=lambda w: (week_counts[w], w))
    return week, week_counts[week]


def _no_baseline(name, threshold, comparison) -> CheckResult:
    return CheckResult(name, None, threshold, comparison, True,
                       {"baseline": None, "note": "first batch for this postal code"})


def total_variation_distance(a: dict, b: dict) -> float:
    """0.5 * sum |p_i - q_i| over the union of categories."""
    ta, tb = sum(a.values()), sum(b.values())
    if not ta or not tb:
        return 0.0 if ta == tb else 1.0
    keys = set(a) | set(b)
    return round(0.5 * sum(abs(a.get(k, 0) / ta - b.get(k, 0) / tb) for k in keys), 4)


def evaluate(current: dict, previous: dict | None) -> list[CheckResult]:
    """Run every check on a batch snapshot. Pure: no I/O."""
    results: list[CheckResult] = []

    # 1. Row count in the dominant flyer week vs the previous batch.
    cur_week, cur_rows = _dominant_week(current.get("week_counts"))
    if previous is None:
        results.append(_no_baseline("row_count_vs_previous", MIN_ROW_CHANGE_PCT, ">="))
    else:
        prev_week, prev_rows = _dominant_week(previous.get("week_counts"))
        change = _pct(cur_rows - prev_rows, prev_rows) if prev_rows else None
        results.append(CheckResult(
            "row_count_vs_previous", change, MIN_ROW_CHANGE_PCT, ">=",
            change is None or change >= MIN_ROW_CHANGE_PCT,
            {"current_week": cur_week, "current_rows": cur_rows,
             "previous_week": prev_week, "previous_rows": prev_rows,
             "previous_batch_id": previous.get("batch_id")},
        ))

    # 2. Chains that disappeared since the previous batch.
    cur_chains = current.get("chain_counts") or {}
    if previous is None:
        results.append(_no_baseline("chain_coverage_vs_previous", MAX_MISSING_CHAINS, "<="))
    else:
        prev_chains = previous.get("chain_counts") or {}
        missing = sorted(c for c in prev_chains if not cur_chains.get(c))
        results.append(CheckResult(
            "chain_coverage_vs_previous", len(missing), MAX_MISSING_CHAINS, "<=",
            len(missing) <= MAX_MISSING_CHAINS,
            {"missing_chains": missing, "current": cur_chains, "previous": prev_chains},
        ))

    # 3. Missing or non-positive prices, measured against everything scraped.
    raw = current.get("raw_row_count") or 0
    unpriced = (raw - (current.get("priced_row_count") or 0)) + (
        current.get("nonpositive_price_count") or 0
    )
    missing_price = _pct(unpriced, raw)
    results.append(CheckResult(
        "missing_or_zero_price_pct", missing_price, MAX_MISSING_PRICE_PCT, "<=",
        missing_price is None or missing_price <= MAX_MISSING_PRICE_PCT,
        {"rows_without_price": unpriced, "raw_rows": raw},
    ))

    # 4. Rows with no parsed weight and no pack count.
    loaded = current.get("loaded_row_count") or 0
    no_size = _pct(current.get("missing_size_count") or 0, loaded)
    results.append(CheckResult(
        "missing_weight_unit_pct", no_size, MAX_MISSING_WEIGHT_UNIT_PCT, "<=",
        no_size is None or no_size <= MAX_MISSING_WEIGHT_UNIT_PCT,
        {"rows_without_size": current.get("missing_size_count"), "loaded_rows": loaded},
    ))

    # 5. Category mix shift vs the previous batch.
    cur_cats = current.get("category_counts") or {}
    if previous is None:
        results.append(_no_baseline("category_distribution_shift", MAX_CATEGORY_TVD, "<="))
    else:
        prev_cats = previous.get("category_counts") or {}
        tvd = total_variation_distance(cur_cats, prev_cats)
        results.append(CheckResult(
            "category_distribution_shift", tvd, MAX_CATEGORY_TVD, "<=",
            tvd <= MAX_CATEGORY_TVD,
            {"current": cur_cats, "previous": prev_cats},
        ))

    # 6. Duplicate grain keys inside the batch (collapsed by the loader; a
    #    spike means the scraper fetched something twice).
    usable = current.get("usable_row_count") or 0
    dup = _pct(current.get("collapsed_row_count") or 0, usable)
    results.append(CheckResult(
        "duplicate_keys_in_batch", dup, MAX_DUPLICATE_PCT, "<=",
        dup is None or dup <= MAX_DUPLICATE_PCT,
        {"collapsed_rows": current.get("collapsed_row_count"), "usable_rows": usable},
    ))

    # 7. Validity windows that end before they start.
    invalid = current.get("invalid_window_count") or 0
    results.append(CheckResult(
        "invalid_validity_window", invalid, MAX_INVALID_WINDOWS, "<=",
        invalid <= MAX_INVALID_WINDOWS, {"rows": invalid},
    ))

    return results


# ---------------------------------------------------------------------------
# Database wrapper
# ---------------------------------------------------------------------------

_SNAPSHOT_COLUMNS = """
    batch_id, postal_code, raw_row_count, priced_row_count, nonpositive_price_count,
    usable_row_count, loaded_row_count, collapsed_row_count, missing_size_count,
    invalid_window_count, category_counts, chain_counts, week_counts
"""


def _fetch_snapshot(cur, sql, params) -> dict | None:
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c.name for c in cur.description], row))


def run_checks(batch_id: int) -> list[CheckResult]:
    """Evaluate, persist and log DQ results for a batch. Never raises."""
    try:
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    current = _fetch_snapshot(
                        cur, f"SELECT {_SNAPSHOT_COLUMNS} FROM etl_batch WHERE batch_id = %s",
                        (batch_id,),
                    )
                    if current is None:
                        logger.error("DQ: batch %s not found", batch_id)
                        return []
                    previous = _fetch_snapshot(
                        cur,
                        f"""
                        SELECT {_SNAPSHOT_COLUMNS} FROM etl_batch
                        WHERE postal_code = %s AND batch_id < %s
                          AND status IN ('success', 'success_with_warnings')
                        ORDER BY batch_id DESC LIMIT 1
                        """,
                        (current["postal_code"], batch_id),
                    )
                    results = evaluate(current, previous)

                    # Name the worst offenders so a failing duplicate check is actionable.
                    cur.execute(
                        """
                        SELECT p.display_name, f.source_row_count
                        FROM fact_price_observation f JOIN dim_product p USING (product_key)
                        WHERE f.last_batch_id = %s AND f.source_row_count > 1
                        ORDER BY f.source_row_count DESC, p.display_name LIMIT 5
                        """,
                        (batch_id,),
                    )
                    top_duplicates = [{"name": n, "rows": c} for n, c in cur.fetchall()]
                    for r in results:
                        if r.check_name == "duplicate_keys_in_batch":
                            r.details["top_duplicates"] = top_duplicates

                    for r in results:
                        cur.execute(
                            """
                            INSERT INTO dq_check_results (batch_id, check_name, metric_value,
                                threshold, comparison, passed, severity, details)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT ON CONSTRAINT uq_dq_batch_check DO UPDATE SET
                                metric_value = EXCLUDED.metric_value,
                                threshold    = EXCLUDED.threshold,
                                comparison   = EXCLUDED.comparison,
                                passed       = EXCLUDED.passed,
                                severity     = EXCLUDED.severity,
                                details      = EXCLUDED.details,
                                created_at   = now()
                            """,
                            (batch_id, r.check_name, r.metric_value, r.threshold,
                             r.comparison, r.passed, r.severity,
                             json.dumps(r.details, default=str)),
                        )
                    failed = [r for r in results if not r.passed]
                    cur.execute(
                        "UPDATE etl_batch SET status = %s WHERE batch_id = %s",
                        ("success_with_warnings" if failed else "success", batch_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception:
        logger.exception("DQ checks crashed for batch %s; the load itself stands", batch_id)
        return []

    for r in failed:
        logger.warning(
            "DQ check failed: batch=%s postal_code=%s check=%s metric=%s (pass when %s %s)",
            batch_id, current["postal_code"], r.check_name, r.metric_value,
            r.comparison, r.threshold,
        )
    return results


def results_as_dicts(results: list[CheckResult]) -> list[dict]:
    return [asdict(r) for r in results]
