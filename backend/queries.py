"""Shared read queries against the flyer_items table.

Centralising these keeps the route handlers thin and ensures search, deals, and
budget mode all read items the same way (and only currently-valid ones).
"""

from datetime import datetime

from database import get_cursor


def _normalize_postal(postal_code: str) -> str:
    return postal_code.replace(" ", "").upper()


def get_items(postal_code: str, merchant: str | None = None) -> list[dict]:
    """Return cleaned items for a postal code (optionally one merchant).

    Only items whose validity window covers today are returned, so search and
    budget mode never surface expired deals.
    """
    postal_code = _normalize_postal(postal_code)
    sql = """
        SELECT id, postal_code, merchant, original_name, clean_name, category,
               price, price_per_kg, price_per_unit, weight_kg,
               valid_from, valid_to, scraped_at
        FROM flyer_items
        WHERE postal_code = %s
          AND (valid_to IS NULL OR valid_to >= %s)
    """
    params: list = [postal_code, datetime.utcnow()]
    if merchant:
        sql += " AND merchant = %s"
        params.append(merchant)
    sql += " ORDER BY clean_name"

    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def get_postal_meta(postal_code: str) -> dict:
    """Return {item_count, scraped_at} for a postal code (0 / None if unseen)."""
    postal_code = _normalize_postal(postal_code)
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n, MAX(scraped_at) AS scraped_at
            FROM flyer_items
            WHERE postal_code = %s
            """,
            (postal_code,),
        )
        row = cur.fetchone()
    return {
        "item_count": row["n"] if row else 0,
        "scraped_at": row["scraped_at"] if row else None,
    }


def get_deal_counts(postal_code: str) -> dict:
    """Return {merchant: count} of currently-valid deals per merchant."""
    postal_code = _normalize_postal(postal_code)
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT merchant, COUNT(*) AS n
            FROM flyer_items
            WHERE postal_code = %s
              AND (valid_to IS NULL OR valid_to >= %s)
            GROUP BY merchant
            """,
            (postal_code, datetime.utcnow()),
        )
        return {r["merchant"]: r["n"] for r in cur.fetchall()}
