"""Shared test helpers: build realistic flyer rows and query results."""

import datetime as dt

from scraper.parser import parse_items

# A real flyer week: Thursday 2026-10-08 to Wednesday 2026-10-14 (Toronto, EDT).
WEEK_1 = dt.date(2026, 10, 8)
WEEK_2 = dt.date(2026, 10, 15)
WEEK_3 = dt.date(2026, 10, 22)


def raw_row(name, price, *, merchant="No Frills", week=WEEK_1, flyer_id=1001,
            valid_from=None, valid_to=None):
    """A row shaped like scraper.flipp.scrape_postal_code() output."""
    end = week + dt.timedelta(days=6)
    return {
        "merchant": merchant,
        "flyer_id": flyer_id,
        "name": name,
        "price": price,
        "valid_from": valid_from if valid_from is not None else f"{week}T00:00:00-04:00",
        "valid_to": valid_to if valid_to is not None else f"{end}T23:59:59-04:00",
    }


def parsed(*rows):
    """Run raw rows through the real parser, exactly as the pipeline does."""
    return parse_items(list(rows))


def query(conn, sql, params=None):
    """Run a SELECT and return rows as dicts."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


# For API-facing tests: a flyer week that is current whenever the suite runs,
# with a validity window comfortably in the future (queries.py hides expired
# deals by comparing against the real clock).
def _this_week():
    from scraper.warehouse import flyer_week_start  # local import keeps helpers light
    return flyer_week_start(dt.date.today())


THIS_WEEK = _this_week()
LAST_MONTH_WEEK = THIS_WEEK - dt.timedelta(weeks=4)


def current_row(name, price, *, merchant="No Frills"):
    return raw_row(name, price, merchant=merchant, week=THIS_WEEK,
                   valid_to=f"{dt.date.today() + dt.timedelta(days=30)}T23:59:59-04:00")
