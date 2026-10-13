"""Weekly flyer refresh: scrape -> load -> data-quality checks, per active postal code.

Production source of truth: .github/workflows/weekly-refresh.yml runs
``python weekly_refresh.py`` every Thursday at 06:00 Toronto time. Locally,
APScheduler calls ``refresh()`` when ENABLE_SCHEDULER=true. You can also run
it by hand: ``python weekly_refresh.py [POSTAL_CODE ...]``.

DST: GitHub's cron has no timezone setting and runs in UTC. 06:00 Toronto is
10:00 UTC under EDT (March-November) and 11:00 UTC under EST. The workflow
registers both crons, and ``should_run_scheduled()`` lets through only the
one that equals 06:00 Toronto on that day, so exactly one run does the work.
It compares the cron expression that fired (github.event.schedule), not the
wall clock, so GitHub's frequent start delays can't make it skip or double up.
(Loads are idempotent, so even a double run would not duplicate data.)

Exit codes: 0 when every postal code loaded, even if DQ checks warned (DQ
failures are warnings by design, surfaced as annotations); 1 when a postal
code failed to load or DATABASE_URL is missing in CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from zoneinfo import ZoneInfo

logger = logging.getLogger("budgetplate.weekly_refresh")

TORONTO = ZoneInfo("America/Toronto")
RUN_HOUR_TORONTO = 6


def expected_utc_hour(day: dt.date) -> int:
    """UTC hour that equals 06:00 in Toronto on ``day`` (10 under EDT, 11 under EST)."""
    local = dt.datetime.combine(day, dt.time(RUN_HOUR_TORONTO), tzinfo=TORONTO)
    return local.astimezone(dt.timezone.utc).hour


def should_run_scheduled(cron: str, now_utc: dt.datetime) -> tuple[bool, str]:
    """Is ``cron`` (e.g. "0 10 * * 4") the one that means 06:00 Toronto today?"""
    try:
        cron_hour = int(cron.split()[1])
    except (IndexError, ValueError):
        # Unknown schedule: run rather than silently skip a week.
        return True, f"unrecognised cron {cron!r}; running"
    today = now_utc.astimezone(TORONTO).date()
    want = expected_utc_hour(today)
    if cron_hour == want:
        return True, f"cron {cron!r} is 06:00 Toronto on {today}"
    return False, (f"cron {cron!r} fires at {cron_hour:02d}:00 UTC but 06:00 Toronto on "
                   f"{today} is {want:02d}:00 UTC; the other cron handles this week")


def refresh(postal_codes: list[str] | None = None,
            triggered_by: str = "scheduled") -> list[dict]:
    """Force-refresh each postal code; one failure doesn't stop the rest."""
    from activity import active_postal_codes
    from scraper.pipeline import run_pipeline

    codes = postal_codes if postal_codes is not None else active_postal_codes()
    logger.info("Weekly refresh: %d postal code(s): %s", len(codes), ", ".join(codes) or "-")
    results = []
    for code in codes:
        try:
            results.append(run_pipeline(code, force=True, triggered_by=triggered_by))
        except Exception as exc:
            logger.exception("Refresh failed for %s", code)
            results.append({"postal_code": code, "status": "error",
                            "message": f"{type(exc).__name__}: {exc}"})
    return results


# ---------------------------------------------------------------------------
# CLI / GitHub Actions entry point
# ---------------------------------------------------------------------------


def _failed_checks(batch_ids: list[int]) -> list[dict]:
    if not batch_ids:
        return []
    from database import get_cursor

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT b.postal_code, d.batch_id, d.check_name, d.metric_value,
                   d.comparison, d.threshold
            FROM dq_check_results d JOIN etl_batch b USING (batch_id)
            WHERE d.batch_id = ANY(%s) AND NOT d.passed
            ORDER BY b.postal_code, d.check_name
            """,
            (batch_ids,),
        )
        return [dict(r) for r in cur.fetchall()]


def _report(results: list[dict], failed: list[dict]) -> str:
    lines = ["## Weekly flyer refresh", "",
             "| Postal code | Result | Batch | Fact rows | DQ |",
             "|---|---|---|---|---|"]
    for r in results:
        dq = ", ".join(r.get("dq_failed_checks") or []) or ("-" if r["status"] == "error" else "all passed")
        lines.append(f"| {r['postal_code']} | {r.get('batch_status') or r['status']} | "
                     f"{r.get('batch_id', '-')} | {r.get('item_count', '-')} | {dq} |")
    if not results:
        lines.append("| - | no active postal codes in the last 30 days | - | - | - |")
    if failed:
        lines += ["", "### Failed data-quality checks (warnings)", "",
                  "| Postal code | Batch | Check | Value | Passes when |", "|---|---|---|---|---|"]
        for f in failed:
            lines.append(f"| {f['postal_code']} | {f['batch_id']} | {f['check_name']} | "
                         f"{f['metric_value']} | {f['comparison']} {f['threshold']} |")
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        lines += ["", "### Load errors", ""] + [f"- **{r['postal_code']}**: {r['message']}"
                                                for r in errors]
    return "\n".join(lines) + "\n"


def _write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Scrape, load and DQ-check flyers.")
    parser.add_argument("postal_codes", nargs="*",
                        help="default: every postal code requested in the last 30 days")
    args = parser.parse_args(argv)

    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    event = os.environ.get("GITHUB_EVENT_NAME", "")

    if event == "schedule":
        run, reason = should_run_scheduled(os.environ.get("SCHEDULE", ""),
                                           dt.datetime.now(dt.timezone.utc))
        print(f"DST gate: {reason}")
        if not run:
            _write_summary(f"## Weekly flyer refresh\n\nSkipped: {reason}.\n")
            return 0

    if in_ci and not os.environ.get("DATABASE_URL"):
        print("::error title=Missing secret::DATABASE_URL repository secret is not set "
              "(Settings > Secrets and variables > Actions).")
        return 1

    # Imported after the gate so a skipped run never touches the database.
    import migrate

    applied = migrate.apply_all()
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")

    requested = args.postal_codes or [
        p.strip() for p in os.environ.get("POSTAL_CODES", "").split(",") if p.strip()
    ]
    codes = [p.replace(" ", "").upper() for p in requested] or None
    results = refresh(codes, triggered_by="github_actions" if in_ci else "manual")

    failed = _failed_checks([r["batch_id"] for r in results if r.get("batch_id")])
    report = _report(results, failed)
    print(report)
    _write_summary(report)

    if in_ci:
        for f in failed:
            print(f"::warning title=DQ {f['postal_code']} {f['check_name']}::"
                  f"value {f['metric_value']} (passes when {f['comparison']} {f['threshold']}), "
                  f"batch {f['batch_id']}")
        for r in results:
            if r["status"] == "error":
                print(f"::error title=Load failed {r['postal_code']}::{r['message']}")

    return 1 if any(r["status"] == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
