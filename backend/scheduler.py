"""Local-development weekly refresh (APScheduler).

Canadian grocery flyers run Thursday to Wednesday (every stored row starts on
a Thursday), so the refresh fires Thursday 06:00 Toronto time, once the new
flyers are live, for every postal code requested in the last 30 days.

Production does NOT use this. Render's free web service sleeps after 15
minutes without traffic, and an in-process job can't fire while it sleeps.
In prod the GitHub Actions workflow (.github/workflows/weekly-refresh.yml) is
the source of truth. This scheduler only starts when ENABLE_SCHEDULER=true,
which .env.example sets for local dev and render.yaml leaves off.

The timezone is explicit: APScheduler otherwise uses the host's zone, which
is Toronto on a dev Mac but UTC on Render, where "6am" fired at 2am Toronto.
"""

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from weekly_refresh import refresh

logger = logging.getLogger("budgetplate.scheduler")

TORONTO = ZoneInfo("America/Toronto")

_scheduler: BackgroundScheduler | None = None


def weekly_trigger() -> CronTrigger:
    """Thursday 06:00 in Toronto, through both EDT and EST."""
    return CronTrigger(day_of_week="thu", hour=6, minute=0, timezone=TORONTO)


def start_scheduler() -> BackgroundScheduler | None:
    """Start the weekly job if ENABLE_SCHEDULER is on. Returns None when off."""
    global _scheduler
    if not settings.ENABLE_SCHEDULER:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER is not true); "
                    "prod refreshes run in GitHub Actions")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=TORONTO)
    _scheduler.add_job(
        refresh,  # same code path as the GitHub Actions job
        weekly_trigger(),
        kwargs={"triggered_by": "scheduled"},
        id="weekly_flyer_refresh",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started: weekly refresh every Thursday 06:00 America/Toronto")
    return _scheduler
