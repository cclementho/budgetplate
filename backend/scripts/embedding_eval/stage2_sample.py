"""Stage 2: sample unmatched products and their closest previous-week candidate (read-only).

The only consecutive flyer weeks in the warehouse are Richmond (V7C4K2) 2026-06-25 and
2026-07-02. From the 07-02 products with no product_key match in the same store's 06-25
flyer, draw a seeded random sample of 50 and, for each, rank that store's 06-25 products
by word overlap (Jaccard over name words, sizes and filler removed).

Usage (from backend/):  python scripts/embedding_eval/stage2_sample.py
Writes scripts/embedding_eval/stage2_sample.csv (top-3 candidates per product).
"""

import csv
import os
import random
import re

import psycopg2
import psycopg2.extras

OUT = os.path.join(os.path.dirname(__file__), "stage2_sample.csv")
DSN = os.environ.get("BASELINE_DATABASE_URL", "postgresql://localhost/budgetplate")
MARKET, WEEK, PREV = "V7C4K2", "2026-07-02", "2026-06-25"
SEED, N = 20261020, 50

FILLER = {"or", "and", "the", "with", "of", "selected", "varieties", "assorted", "product",
          "pk", "pack", "each", "ea", "size", "sizes", "kg", "g", "lb", "ml", "l", "oz"}


def words(name: str) -> set[str]:
    w = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    return {t for t in w if t not in FILLER and not re.fullmatch(r"\d+(?:\w{0,2})?", t)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def main():
    cur = psycopg2.connect(DSN).cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT f.product_key, f.store_key, f.flyer_week_start::text AS week, f.price, f.original_name,
               s.chain, p.display_name, p.normalized_name, p.category, p.weight_kg, p.pack_count
        FROM fact_price_observation f
        JOIN dim_store s USING (store_key) JOIN dim_product p USING (product_key)
        WHERE s.market_postal_code = %s AND f.flyer_week_start IN (%s, %s)
    """, (MARKET, WEEK, PREV))
    rows = cur.fetchall()
    prev = [r for r in rows if r["week"] == PREV]
    prev_keys = {(r["store_key"], r["product_key"]) for r in prev}
    unmatched = sorted(
        (r for r in rows if r["week"] == WEEK and (r["store_key"], r["product_key"]) not in prev_keys),
        key=lambda r: (r["chain"], r["product_key"]),
    )
    sample = random.Random(SEED).sample(unmatched, N)

    def size(r):
        return f"{float(r['weight_kg']):g} kg" if r["weight_kg"] is not None else "-", r["pack_count"] or "-"

    out = []
    for i, r in enumerate(sorted(sample, key=lambda r: (r["chain"], r["display_name"])), 1):
        w = words(r["normalized_name"])
        cands = sorted(
            (c for c in prev if c["store_key"] == r["store_key"]),
            key=lambda c: jaccard(w, words(c["normalized_name"])), reverse=True,
        )[:3]
        row = {"n": i, "chain": r["chain"], "jul02_name": r["display_name"],
               "jul02_original": r["original_name"], "jul02_category": r["category"],
               "jul02_size": "%s / pack %s" % size(r), "jul02_price": float(r["price"])}
        for k, c in enumerate(cands, 1):
            row |= {f"c{k}_name": c["display_name"], f"c{k}_original": c["original_name"],
                    f"c{k}_category": c["category"], f"c{k}_size": "%s / pack %s" % size(c),
                    f"c{k}_price": float(c["price"]),
                    f"c{k}_overlap": round(jaccard(w, words(c["normalized_name"])), 2)}
        out.append(row)

    with open(OUT, "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=list(out[0]))
        wtr.writeheader()
        wtr.writerows(out)
    print(f"unmatched on {WEEK}: {len(unmatched)}; sampled {N} (seed {SEED}) -> {OUT}")
    for r in out:
        print(f"\n#{r['n']} [{r['chain']}] {r['jul02_name']}  ({r['jul02_size']}, ${r['jul02_price']}, {r['jul02_category']})")
        print(f"    orig: {r['jul02_original']}")
        for k in (1, 2, 3):
            if f"c{k}_name" in r:
                print(f"    c{k} {r[f'c{k}_overlap']:.2f}: {r[f'c{k}_name']}  ({r[f'c{k}_size']}, ${r[f'c{k}_price']}) | orig: {r[f'c{k}_original']}")


if __name__ == "__main__":
    main()
