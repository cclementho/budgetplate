"""Backfill: load the legacy flyer_items rows as the warehouse's first batches.

Before the warehouse existed, each scrape overwrote flyer_items, so all that
survives is the latest scrape per postal code. Migration 003 renamed that
table to flyer_items_legacy; this script loads it, one batch per postal code.

Two deliberate choices:

* Re-parse, don't copy. The legacy clean_name/category came from an earlier
  parser: today's parser disagrees on 43% of names and 33% of categories.
  Copying them would stop July products from matching their October rows. So
  only the raw inputs (original_name, price, validity dates) are kept, and
  they go through the current parser, the same code path as a live scrape.
* Eastern time. The legacy columns are TIMESTAMP without time zone, and
  Postgres silently drops the offset of a literal like
  '2026-07-02T03:00:00-04:00', keeping the wall clock. Flipp reports every
  market in Eastern time (BC flyers start at 03:00-04:00 = midnight Pacific),
  so the wall clock is read back as America/Toronto.

Idempotent: postal codes that already have a successful backfill batch are
skipped unless --force is given. Even with --force, facts are upserted, so
nothing duplicates.

Usage:  python backfill.py [--force] [POSTAL_CODE ...]
"""

import argparse
import logging
import sys
from zoneinfo import ZoneInfo

from database import get_cursor
from scraper.parser import parse_items
from scraper.warehouse import load_batch, normalize_postal

logger = logging.getLogger("budgetplate.backfill")

FLIPP_TZ = ZoneInfo("America/Toronto")
NOTES = ("Backfilled from flyer_items_legacy; original_name re-parsed with the "
         "current parser; naive timestamps read as America/Toronto (Flipp's zone).")


def _legacy_postal_codes() -> list[str]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('flyer_items_legacy') IS NOT NULL AS present
            """
        )
        if not cur.fetchone()["present"]:
            return []
        cur.execute("SELECT DISTINCT postal_code FROM flyer_items_legacy ORDER BY 1")
        return [r["postal_code"] for r in cur.fetchall()]


def _already_backfilled(postal_code: str) -> bool:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM etl_batch
                WHERE postal_code = %s AND source = 'backfill'
                  AND status IN ('success', 'success_with_warnings')
            ) AS done
            """,
            (postal_code,),
        )
        return cur.fetchone()["done"]


def _legacy_rows(postal_code: str) -> list[dict]:
    """Legacy rows reshaped as scraper output (the parser's input)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT merchant, original_name, price, valid_from, valid_to
            FROM flyer_items_legacy
            WHERE postal_code = %s
            ORDER BY id
            """,
            (postal_code,),
        )
        return [
            {
                "merchant": r["merchant"],
                "flyer_id": None,  # never persisted by the legacy pipeline
                "name": r["original_name"],
                "price": r["price"],
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
            }
            for r in cur.fetchall()
        ]


def run(postal_codes: list[str] | None = None, force: bool = False) -> list[dict]:
    """Backfill the given postal codes (default: all in the legacy table)."""
    codes = [normalize_postal(p) for p in postal_codes] if postal_codes else _legacy_postal_codes()
    results = []
    for code in codes:
        if not force and _already_backfilled(code):
            results.append({"postal_code": code, "status": "skipped",
                             "message": "already backfilled (use --force to reload)"})
            continue
        raw = _legacy_rows(code)
        if not raw:
            results.append({"postal_code": code, "status": "skipped",
                            "message": "no legacy rows"})
            continue
        summary = load_batch(
            code,
            parse_items(raw),
            raw_row_count=len(raw),
            source="backfill",
            triggered_by="backfill",
            naive_tz=FLIPP_TZ,
            notes=NOTES,
        )
        results.append(summary)
    return results


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("postal_codes", nargs="*", help="default: every legacy postal code")
    parser.add_argument("--force", action="store_true",
                        help="reload postal codes that were already backfilled")
    args = parser.parse_args(argv)

    results = run(args.postal_codes, force=args.force)
    if not results:
        print("Nothing to backfill: flyer_items_legacy is missing or empty.")
    for r in results:
        if r.get("batch_id"):
            weeks = f"{r['min_flyer_week']} .. {r['max_flyer_week']}"
            failed = ", ".join(r["dq_failed_checks"]) or "none"
            print(f"{r['postal_code']}: batch {r['batch_id']} {r['status']} | "
                  f"{r['raw_row_count']} legacy rows -> {r['loaded_row_count']} facts "
                  f"({r['collapsed_row_count']} duplicates collapsed) | weeks {weeks} | "
                  f"DQ failed: {failed}")
        else:
            print(f"{r['postal_code']}: {r['status']} ({r['message']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
