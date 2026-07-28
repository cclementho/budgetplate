"""Background refresh job.

Canadian grocery flyers typically reset on Wednesday, so we re-run the pipeline
every Wednesday at 6am for any postal code searched in the last 30 days. This
keeps the cache warm without scraping postal codes nobody is using.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scraper.pipeline import recent_postal_codes, run_pipeline

logger = logging.getLogger("budgetplate.scheduler")

_scheduler: BackgroundScheduler | None = None


def refresh_recent_postal_codes() -> None:
    """Force-refresh every postal code searched in the last 30 days."""
    codes = recent_postal_codes(days=30)
    logger.info("Weekly refresh: %d postal codes to update", len(codes))
    for code in codes:
        try:
            result = run_pipeline(code, force=True)
            logger.info("Refreshed %s: %s", code, result.get("status"))
        except Exception as exc:  # keep going if one postal code fails
            logger.exception("Refresh failed for %s: %s", code, exc)


def start_scheduler() -> BackgroundScheduler:
    """Start the APScheduler job (Wednesdays at 6am, server local time)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        refresh_recent_postal_codes,
        CronTrigger(day_of_week="wed", hour=6, minute=0),
        id="weekly_flyer_refresh",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started: weekly refresh every Wed 06:00")
    return _scheduler
