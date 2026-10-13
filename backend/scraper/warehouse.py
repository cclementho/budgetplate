"""Load parsed flyer rows into the price-history star schema.

``load_batch()`` turns one scrape (or one backfill slice) into:

* one ``etl_batch`` row, an append-only audit record of the attempt;
* upserted ``dim_store`` / ``dim_product`` rows;
* upserted ``fact_price_observation`` rows at the grain
  product x store-market x flyer week.

Idempotency: facts are written with ``INSERT ... ON CONFLICT`` on that grain, so
re-loading the same postal code + flyer week refreshes rows in place instead
of duplicating them. ``etl_batch`` still records every attempt.

Duplicates inside one batch: Flipp often lists the same product several times
in one flyer (identical name, sometimes different prices for variants). Rows
that share a grain key collapse into one fact: ``price`` = lowest,
``price_max`` = highest, ``source_row_count`` = how many rows merged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg2.extras

from database import get_conn

logger = logging.getLogger("budgetplate.warehouse")

# Must match the dim_date range populated by migration 001.
DIM_DATE_MIN = dt.date(2020, 1, 1)
DIM_DATE_MAX = dt.date(2035, 12, 31)

DEFAULT_TZ = ZoneInfo("America/Toronto")

_THURSDAY = 3  # date.weekday(): Monday=0 ... Thursday=3
_NON_WORD = re.compile(r"[^\w]+")
_KG = Decimal("0.001")
_CENT = Decimal("0.01")


def flyer_week_start(day: dt.date) -> dt.date:
    """Thursday on/before ``day``. Canadian grocery flyers run Thursday to Wednesday."""
    return day - dt.timedelta(days=(day.weekday() - _THURSDAY) % 7)


def normalize_name(name: str) -> str:
    """Product matching key: lowercase, punctuation to spaces, whitespace collapsed."""
    return " ".join(_NON_WORD.sub(" ", (name or "").lower()).split())


def normalize_postal(postal_code: str) -> str:
    return (postal_code or "").replace(" ", "").upper()


# ---------------------------------------------------------------------------
# Row preparation (pure Python, no database)
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    """One usable source row, typed and keyed for loading."""

    chain: str
    normalized_name: str
    display_name: str
    category: str
    weight_kg: Decimal | None
    pack_count: int | None
    size_unit: str | None
    flyer_week: dt.date
    valid_from: dt.datetime
    valid_to: dt.datetime | None
    price: Decimal
    price_per_kg: Decimal | None
    price_per_unit: Decimal | None
    unit_type: str
    unit_price: Decimal | None
    original_name: str
    flyer_id: int | None

    @property
    def product_key(self) -> tuple:
        return (self.chain, self.normalized_name, self.weight_kg, self.pack_count)

    @property
    def grain_key(self) -> tuple:
        return (*self.product_key, self.flyer_week)


def _dec(value, quantum: Decimal) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(quantum)


def _to_aware(value, naive_tz: ZoneInfo) -> dt.datetime | None:
    """Parse a flyer timestamp. Naive values are interpreted in ``naive_tz``."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed


def _in_dim_date(day: dt.date | None) -> bool:
    return day is None or DIM_DATE_MIN <= day <= DIM_DATE_MAX


def _prepare(parsed_rows: list[dict], naive_tz: ZoneInfo) -> tuple[list[_Row], Counter]:
    """Type, validate and key source rows. Returns (usable rows, counters)."""
    usable: list[_Row] = []
    counts: Counter = Counter()

    for r in parsed_rows:
        price = _dec(r.get("price"), _CENT)
        if price is None or price <= 0:
            counts["nonpositive_price"] += 1
            counts["dropped"] += 1
            continue

        valid_from = _to_aware(r.get("valid_from"), naive_tz)
        valid_to = _to_aware(r.get("valid_to"), naive_tz)
        if valid_from is None:  # can't place the row in a flyer week
            counts["dropped"] += 1
            continue
        # The flyer week is the flyer's own local date, not the UTC date.
        if not (_in_dim_date(valid_from.date()) and _in_dim_date(valid_to and valid_to.date())):
            counts["dropped"] += 1
            continue
        if valid_to is not None and valid_to < valid_from:
            counts["invalid_window"] += 1  # kept, but flagged by DQ

        weight = _dec(r.get("weight_kg"), _KG)
        if weight is not None and weight <= 0:
            weight = None
        pack = r.get("pack_count") or None

        if weight is not None:
            unit_type, unit_price = "kg", r.get("price_per_kg")
        elif pack:
            unit_type, unit_price = "pack_unit", r.get("price_per_unit")
        else:
            unit_type, unit_price = "item", r.get("price_per_unit")

        display = r.get("clean_name") or r.get("original_name") or ""
        usable.append(
            _Row(
                chain=r.get("merchant") or "Unknown",
                normalized_name=normalize_name(display),
                display_name=display,
                category=r.get("category") or "Other",
                weight_kg=weight,
                pack_count=pack,
                size_unit=r.get("size_unit"),
                flyer_week=flyer_week_start(valid_from.date()),
                valid_from=valid_from,
                valid_to=valid_to,
                price=price,
                price_per_kg=_dec(r.get("price_per_kg"), _CENT),
                price_per_unit=_dec(r.get("price_per_unit"), _CENT),
                unit_type=unit_type,
                unit_price=_dec(unit_price, _CENT),
                original_name=r.get("original_name") or display,
                flyer_id=r.get("flyer_id"),
            )
        )
    return usable, counts


def _collapse(rows: list[_Row]) -> list[dict]:
    """Merge rows that share a grain key into one fact.

    The cheapest row is the representative (its name, flyer, unit price);
    price_max and source_row_count record what was merged. The validity
    window widens to cover every merged row.
    """
    groups: dict[tuple, list[_Row]] = {}
    for row in rows:
        groups.setdefault(row.grain_key, []).append(row)

    facts = []
    for group in groups.values():
        rep = min(group, key=lambda r: r.price)  # first cheapest wins ties
        valid_tos = [r.valid_to for r in group if r.valid_to is not None]
        facts.append(
            {
                "row": rep,
                "price": rep.price,
                "price_max": max(r.price for r in group),
                "valid_from": min(r.valid_from for r in group),
                "valid_to": max(valid_tos) if valid_tos else None,
                "source_row_count": len(group),
            }
        )
    return facts


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------


def _open_batch(postal_code, source, triggered_by, raw_count, priced_count, notes) -> int:
    """Insert the etl_batch row in its own transaction, so a load that later
    fails still leaves an audit record behind."""
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO etl_batch (postal_code, source, triggered_by,
                                           raw_row_count, priced_row_count, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING batch_id
                    """,
                    (postal_code, source, triggered_by, raw_count, priced_count, notes),
                )
                batch_id = cur.fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return batch_id


def _fail_batch(batch_id: int, exc: Exception) -> None:
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE etl_batch
                    SET status = 'failed', finished_at = now(), error = %s
                    WHERE batch_id = %s
                    """,
                    (f"{type(exc).__name__}: {exc}"[:2000], batch_id),
                )
            conn.commit()
        except Exception:  # never mask the original error
            conn.rollback()
            logger.exception("Could not mark batch %s as failed", batch_id)


def _upsert_stores(cur, postal_code: str, rows: list[_Row]) -> dict[str, int]:
    weeks: dict[str, list[dt.date]] = {}
    for r in rows:
        weeks.setdefault(r.chain, []).append(r.flyer_week)
    values = [(chain, postal_code, min(w), max(w)) for chain, w in weeks.items()]
    if not values:
        return {}
    result = psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO dim_store (chain, market_postal_code, first_seen_week, last_seen_week)
        VALUES %s
        ON CONFLICT ON CONSTRAINT uq_dim_store DO UPDATE SET
            first_seen_week = LEAST(dim_store.first_seen_week, EXCLUDED.first_seen_week),
            last_seen_week  = GREATEST(dim_store.last_seen_week, EXCLUDED.last_seen_week)
        RETURNING chain, store_key
        """,
        values,
        fetch=True,
    )
    return {chain: key for chain, key in result}


def _upsert_products(cur, rows: list[_Row]) -> dict[tuple, int]:
    # One VALUES row per natural key (ON CONFLICT can't touch a row twice in
    # one statement). Names/category come from the latest week in the batch.
    latest: dict[tuple, _Row] = {}
    span: dict[tuple, tuple[dt.date, dt.date]] = {}
    for r in rows:
        k = r.product_key
        if k not in latest or r.flyer_week >= latest[k].flyer_week:
            latest[k] = r
        lo, hi = span.get(k, (r.flyer_week, r.flyer_week))
        span[k] = (min(lo, r.flyer_week), max(hi, r.flyer_week))

    values = [
        (r.chain, r.normalized_name, r.display_name, r.category, r.weight_kg,
         r.pack_count, r.size_unit, span[k][0], span[k][1])
        for k, r in latest.items()
    ]
    if not values:
        return {}
    result = psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO dim_product (chain, normalized_name, display_name, category,
                                 weight_kg, pack_count, size_unit,
                                 first_seen_week, last_seen_week)
        VALUES %s
        ON CONFLICT ON CONSTRAINT uq_dim_product_natural DO UPDATE SET
            -- Descriptive attributes follow the most recent week seen
            -- (type 1 SCD), so loading an old batch can't overwrite them.
            display_name = CASE WHEN EXCLUDED.last_seen_week >= dim_product.last_seen_week
                                THEN EXCLUDED.display_name ELSE dim_product.display_name END,
            category     = CASE WHEN EXCLUDED.last_seen_week >= dim_product.last_seen_week
                                THEN EXCLUDED.category ELSE dim_product.category END,
            size_unit    = COALESCE(EXCLUDED.size_unit, dim_product.size_unit),
            first_seen_week = LEAST(dim_product.first_seen_week, EXCLUDED.first_seen_week),
            last_seen_week  = GREATEST(dim_product.last_seen_week, EXCLUDED.last_seen_week)
        RETURNING chain, normalized_name, weight_kg, pack_count, product_key
        """,
        values,
        page_size=1000,
        fetch=True,
    )
    return {(c, n, w, p): key for c, n, w, p, key in result}


def _upsert_facts(cur, batch_id, facts, store_keys, product_keys) -> tuple[int, int]:
    values = []
    for f in facts:
        r: _Row = f["row"]
        values.append(
            (
                product_keys[r.product_key],
                store_keys[r.chain],
                r.flyer_week,
                f["valid_from"].date(),
                f["valid_to"].date() if f["valid_to"] else None,
                f["valid_from"],
                f["valid_to"],
                f["price"],
                f["price_max"],
                r.unit_price,
                r.unit_type,
                r.price_per_kg,
                r.price_per_unit,
                r.weight_kg,
                r.original_name,
                r.flyer_id,
                f["source_row_count"],
                batch_id,  # first_batch_id: only used when the row is new
                batch_id,  # last_batch_id
            )
        )
    if not values:
        return 0, 0
    result = psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO fact_price_observation (
            product_key, store_key, flyer_week_start, valid_from_date, valid_to_date,
            valid_from, valid_to, price, price_max, unit_price, unit_type,
            price_per_kg, price_per_unit, weight_kg, original_name, flyer_id,
            source_row_count, first_batch_id, last_batch_id)
        VALUES %s
        ON CONFLICT ON CONSTRAINT uq_fact_grain DO UPDATE SET
            valid_from_date  = EXCLUDED.valid_from_date,
            valid_to_date    = EXCLUDED.valid_to_date,
            valid_from       = EXCLUDED.valid_from,
            valid_to         = EXCLUDED.valid_to,
            price            = EXCLUDED.price,
            price_max        = EXCLUDED.price_max,
            unit_price       = EXCLUDED.unit_price,
            unit_type        = EXCLUDED.unit_type,
            price_per_kg     = EXCLUDED.price_per_kg,
            price_per_unit   = EXCLUDED.price_per_unit,
            weight_kg        = EXCLUDED.weight_kg,
            original_name    = EXCLUDED.original_name,
            flyer_id         = EXCLUDED.flyer_id,
            source_row_count = EXCLUDED.source_row_count,
            last_batch_id    = EXCLUDED.last_batch_id
            -- first_batch_id deliberately untouched: it records first sighting.
        RETURNING (xmax = 0) AS inserted
        """,
        values,
        page_size=1000,
        fetch=True,
    )
    inserted = sum(1 for (was_insert,) in result if was_insert)
    return inserted, len(result) - inserted


def load_batch(
    postal_code: str,
    parsed_rows: list[dict],
    *,
    raw_row_count: int | None = None,
    source: str = "scrape",
    triggered_by: str = "on_demand",
    naive_tz: ZoneInfo = DEFAULT_TZ,
    notes: str | None = None,
) -> dict:
    """Load one batch of parsed rows. Returns a summary of what was written.

    ``parsed_rows`` are scraper/parser.parse_item() dicts. ``raw_row_count`` is
    how many rows the scraper returned before rows without a price were
    dropped (defaults to ``len(parsed_rows)``). Naive timestamps are read in
    ``naive_tz``; live Flipp timestamps carry their own offset.
    """
    postal_code = normalize_postal(postal_code)
    raw_count = len(parsed_rows) if raw_row_count is None else raw_row_count
    batch_id = _open_batch(
        postal_code, source, triggered_by, raw_count, len(parsed_rows), notes
    )

    try:
        usable, counts = _prepare(parsed_rows, naive_tz)
        facts = _collapse(usable)
        lock_name = f"budgetplate:load:{postal_code}"

        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    # Serialize concurrent loads of the same market (e.g. a user's
                    # on-demand scrape racing the weekly job).
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_name,))
                    store_keys = _upsert_stores(cur, postal_code, usable)
                    product_keys = _upsert_products(cur, usable)
                    inserted, updated = _upsert_facts(
                        cur, batch_id, facts, store_keys, product_keys
                    )

                    weeks = [f["row"].flyer_week for f in facts]
                    stats = {
                        "nonpositive_price_count": counts["nonpositive_price"],
                        "dropped_row_count": counts["dropped"],
                        "usable_row_count": len(usable),
                        "loaded_row_count": len(facts),
                        "inserted_row_count": inserted,
                        "updated_row_count": updated,
                        "collapsed_row_count": len(usable) - len(facts),
                        "missing_size_count": sum(
                            1 for f in facts
                            if f["row"].weight_kg is None and f["row"].pack_count is None
                        ),
                        "invalid_window_count": counts["invalid_window"],
                        "category_counts": dict(Counter(f["row"].category for f in facts)),
                        "chain_counts": dict(Counter(f["row"].chain for f in facts)),
                        "min_flyer_week": min(weeks) if weeks else None,
                        "max_flyer_week": max(weeks) if weeks else None,
                    }
                    cur.execute(
                        """
                        UPDATE etl_batch SET
                            status = 'success', finished_at = now(),
                            nonpositive_price_count = %(nonpositive_price_count)s,
                            dropped_row_count = %(dropped_row_count)s,
                            usable_row_count = %(usable_row_count)s,
                            loaded_row_count = %(loaded_row_count)s,
                            inserted_row_count = %(inserted_row_count)s,
                            updated_row_count = %(updated_row_count)s,
                            collapsed_row_count = %(collapsed_row_count)s,
                            missing_size_count = %(missing_size_count)s,
                            invalid_window_count = %(invalid_window_count)s,
                            category_counts = %(category_counts_json)s,
                            chain_counts = %(chain_counts_json)s,
                            min_flyer_week = %(min_flyer_week)s,
                            max_flyer_week = %(max_flyer_week)s
                        WHERE batch_id = %(batch_id)s
                        """,
                        {
                            **stats,
                            "category_counts_json": json.dumps(stats["category_counts"]),
                            "chain_counts_json": json.dumps(stats["chain_counts"]),
                            "batch_id": batch_id,
                        },
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        _fail_batch(batch_id, exc)
        raise

    logger.info(
        "Batch %s (%s): %s facts (%s new, %s refreshed) from %s usable rows",
        batch_id, postal_code, stats["loaded_row_count"], inserted, updated, len(usable),
    )
    return {
        "batch_id": batch_id,
        "postal_code": postal_code,
        "status": "success",
        "raw_row_count": raw_count,
        "priced_row_count": len(parsed_rows),
        **stats,
    }
