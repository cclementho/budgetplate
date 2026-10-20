"""Stage 1: exact baseline match rates for today's product matching (read-only).

Today a "product" is one dim_product row: same chain + normalized name + weight_kg +
pack_count. Two observations "match" when they share that product_key. Every query
here is a SELECT; nothing in the warehouse is changed.

Usage (from backend/):  python scripts/embedding_eval/stage1_baseline.py
Writes scripts/embedding_eval/stage1_baseline.json
"""

import json
import os
from collections import defaultdict
from datetime import timedelta

import psycopg2
import psycopg2.extras

OUT = os.path.join(os.path.dirname(__file__), "stage1_baseline.json")
DSN = os.environ.get("BASELINE_DATABASE_URL", "postgresql://localhost/budgetplate")


def load(cur):
    cur.execute("""
        SELECT f.product_key, f.store_key, f.flyer_week_start AS week, f.is_ambiguous,
               f.unit_price, s.market_postal_code AS market, s.chain
        FROM fact_price_observation f JOIN dim_store s USING (store_key)
    """)
    return cur.fetchall()


def week_over_week(rows, consecutive_only: bool):
    """Per store-week: share of its products also present in that store's previous week.

    consecutive_only=True  -> previous week must be exactly 7 days earlier.
    consecutive_only=False -> previous week = the store's most recent earlier flyer week.
    """
    by_store_week = defaultdict(set)
    for r in rows:
        by_store_week[r["store_key"], r["week"]].add(r["product_key"])
    weeks_by_store = defaultdict(list)
    store_info = {}
    for (store, week) in by_store_week:
        weeks_by_store[store].append(week)
    for r in rows:
        store_info[r["store_key"]] = (r["market"], r["chain"])

    out = []
    for store, weeks in weeks_by_store.items():
        weeks.sort()
        for prev, cur in zip(weeks, weeks[1:]):
            if consecutive_only and cur - prev != timedelta(days=7):
                continue
            products = by_store_week[store, cur]
            matched = products & by_store_week[store, prev]
            market, chain = store_info[store]
            out.append({
                "market": market, "chain": chain, "week": str(cur), "previous_week": str(prev),
                "weeks_apart": (cur - prev).days // 7, "products": len(products),
                "matched": len(matched), "rate": len(matched) / len(products),
                "previous_week_products": len(by_store_week[store, prev]),
            })
    out.sort(key=lambda x: (x["market"], x["chain"], x["week"]))
    return out


def summarize(pairs):
    if not pairs:
        return None
    return {
        "store_weeks": len(pairs),
        "mean_rate": sum(p["rate"] for p in pairs) / len(pairs),          # each store-week counts equally
        "pooled_rate": sum(p["matched"] for p in pairs) / sum(p["products"] for p in pairs),
        "matched": sum(p["matched"] for p in pairs),
        "products": sum(p["products"] for p in pairs),
    }


def history(rows):
    """Share of price series (product at one store) seen in 2+ flyer weeks."""
    weeks = defaultdict(set)
    weeks_any_store = defaultdict(set)
    for r in rows:
        weeks[r["product_key"], r["store_key"]].add(r["week"])
        weeks_any_store[r["product_key"]].add(r["week"])
    series = len(weeks)
    multi = sum(1 for w in weeks.values() if len(w) >= 2)
    prods = len(weeks_any_store)
    multi_any = sum(1 for w in weeks_any_store.values() if len(w) >= 2)
    all_weeks = sorted({r["week"] for r in rows})
    return {
        "first_week": str(all_weeks[0]), "last_week": str(all_weeks[-1]),
        "distinct_weeks": [str(w) for w in all_weeks],
        "series": series, "series_with_2plus_weeks": multi, "series_rate": multi / series,
        "products": prods, "products_with_2plus_weeks": multi_any, "products_rate": multi_any / prods,
    }


def toronto_oct8(rows):
    """Analytics Q3's definition, plus an all-rows variant."""
    import datetime as dt
    week = dt.date(2026, 10, 8)
    t = [r for r in rows if r["market"] == "M5S1Z9"]
    earlier_clean = {(r["product_key"], r["store_key"]) for r in t
                     if r["week"] < week and not r["is_ambiguous"] and r["unit_price"] is not None}
    earlier_any = {(r["product_key"], r["store_key"]) for r in t if r["week"] < week}
    adv = [r for r in t if r["week"] == week and not r["is_ambiguous"] and r["unit_price"] is not None]
    adv_all = [r for r in t if r["week"] == week]
    return {
        "q3_definition": {
            "advertised": len(adv),
            "with_earlier_history": sum((r["product_key"], r["store_key"]) in earlier_clean for r in adv),
        },
        "all_rows": {
            "advertised": len(adv_all),
            "with_earlier_history": sum((r["product_key"], r["store_key"]) in earlier_any for r in adv_all),
        },
    }


def main():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rows = load(cur)
    consecutive = week_over_week(rows, consecutive_only=True)
    previous = week_over_week(rows, consecutive_only=False)
    result = {
        "facts": len(rows),
        "m1_consecutive_weeks": {"pairs": consecutive, "summary": summarize(consecutive)},
        "m1b_previous_available_week": {"pairs": previous, "summary": summarize(previous)},
        "m2_history": history(rows),
        "m3_toronto_oct8": toronto_oct8(rows),
    }
    with open(OUT, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
