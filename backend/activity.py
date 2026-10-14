"""Postal-code demand: who is actually using which market.

The API records a request here whenever someone loads results for a postal
code, and the weekly refresh scrapes only postal codes requested recently.
Keeping demand separate from scrape history stops the refresh from keeping
its own postal codes alive forever.
"""

import logging

from database import get_cursor

logger = logging.getLogger("budgetplate.activity")

ACTIVE_DAYS = 30


def _normalize(postal_code: str) -> str:
    return (postal_code or "").replace(" ", "").upper()


def record_request(postal_code: str) -> None:
    """Note that someone asked for this postal code. Never raises: losing one
    activity ping must not break the user's request."""
    try:
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO postal_code_activity (postal_code)
                VALUES (%s)
                ON CONFLICT (postal_code) DO UPDATE SET
                    last_requested_at = now(),
                    request_count     = postal_code_activity.request_count + 1
                """,
                (_normalize(postal_code),),
            )
    except Exception:
        logger.exception("Could not record activity for %s", postal_code)


def active_postal_codes(days: int = ACTIVE_DAYS) -> list[str]:
    """Postal codes requested within the last ``days`` days."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT postal_code FROM postal_code_activity
            WHERE last_requested_at >= now() - make_interval(days => %s)
            ORDER BY postal_code
            """,
            (days,),
        )
        return [r["postal_code"] for r in cur.fetchall()]
