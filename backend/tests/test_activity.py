"""Postal-code demand tracking, which decides what the weekly refresh scrapes."""

import activity
from routes import stores as stores_route
from scraper import pipeline
from scraper.pipeline import run_pipeline
from tests.helpers import current_row, query


def test_requests_are_counted(db):
    activity.record_request("m5s 1z9")
    activity.record_request("M5S1Z9")
    assert query(db, "SELECT postal_code, request_count FROM postal_code_activity") == [
        {"postal_code": "M5S1Z9", "request_count": 2}
    ]


def test_only_recent_requests_are_active(db):
    activity.record_request("M5S1Z9")
    activity.record_request("V7C4K2")
    with db.cursor() as cur:
        cur.execute("UPDATE postal_code_activity SET last_requested_at = now() - interval '40 days' "
                    "WHERE postal_code = 'V7C4K2'")
    db.commit()
    assert activity.active_postal_codes(days=30) == ["M5S1Z9"]


def test_scheduled_scrapes_do_not_keep_a_postal_code_alive(monkeypatch):
    """Regression: active-ness used to be read from scrape times, so the weekly
    refresh kept every postal code it had ever scraped active forever."""
    monkeypatch.setattr(pipeline, "scrape_postal_code",
                        lambda pc: [current_row("BOK CHOY", "1.99")])
    run_pipeline("M5S1Z9", force=True, triggered_by="scheduled")
    assert activity.active_postal_codes() == []


def test_the_stores_endpoint_records_demand(monkeypatch):
    monkeypatch.setattr(stores_route, "start_background_scrape", lambda pc: True)
    response = stores_route.stores(postal_code="m5s 1z9")  # no data yet -> loading
    assert response["status"] == "loading"
    assert activity.active_postal_codes() == ["M5S1Z9"]


def test_a_failed_activity_write_never_breaks_the_request(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(activity, "get_cursor", broken)
    activity.record_request("M5S1Z9")  # must not raise
    assert "Could not record activity" in caplog.text
