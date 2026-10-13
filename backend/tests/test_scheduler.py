"""Local APScheduler: Thursday 06:00 Toronto, through DST, and opt-in only."""

import datetime as dt

import scheduler
from config import settings

UTC = dt.timezone.utc


def _next_fire(after_utc):
    return scheduler.weekly_trigger().get_next_fire_time(None, after_utc)


def test_fires_thursday_6am_toronto_in_summer_time():
    # Monday 2026-10-12 (EDT, UTC-4) -> Thursday 2026-10-15 06:00 EDT = 10:00 UTC
    fire = _next_fire(dt.datetime(2026, 10, 12, 12, 0, tzinfo=UTC))
    assert fire.astimezone(UTC) == dt.datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def test_fires_thursday_6am_toronto_in_winter_time():
    # Monday 2026-11-16 (EST, UTC-5) -> Thursday 2026-11-19 06:00 EST = 11:00 UTC
    fire = _next_fire(dt.datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
    assert fire.astimezone(UTC) == dt.datetime(2026, 11, 19, 11, 0, tzinfo=UTC)


def test_independent_of_the_host_timezone(monkeypatch):
    # Render containers run in UTC; the trigger must not follow the host.
    monkeypatch.setenv("TZ", "UTC")
    fire = _next_fire(dt.datetime(2026, 10, 12, 12, 0, tzinfo=UTC))
    assert fire.astimezone(scheduler.TORONTO).hour == 6
    assert fire.astimezone(scheduler.TORONTO).strftime("%A") == "Thursday"


def test_scheduler_is_off_unless_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_SCHEDULER", False)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    assert scheduler.start_scheduler() is None


def test_scheduler_starts_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_SCHEDULER", True)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    started = scheduler.start_scheduler()
    try:
        job = started.get_job("weekly_flyer_refresh")
        assert str(job.trigger.timezone) == "America/Toronto"
        assert job.next_run_time.astimezone(scheduler.TORONTO).strftime("%A %H:%M") == "Thursday 06:00"
    finally:
        started.shutdown(wait=False)
