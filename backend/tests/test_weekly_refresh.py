"""Weekly refresh entry point: DST gate, CLI behaviour, and the workflow file."""

import datetime as dt
import os

import pytest
import yaml

import migrate
import weekly_refresh
from scraper import pipeline
from weekly_refresh import expected_utc_hour, should_run_scheduled
from tests.helpers import current_row, query

UTC = dt.timezone.utc
SUMMER_CRON, WINTER_CRON = "0 10 * * 4", "0 11 * * 4"
WORKFLOW = os.path.join(os.path.dirname(__file__), "..", "..", ".github",
                        "workflows", "weekly-refresh.yml")


# --- DST gate -----------------------------------------------------------------

def test_expected_utc_hour_follows_toronto_dst():
    assert expected_utc_hour(dt.date(2026, 10, 15)) == 10  # EDT, UTC-4
    assert expected_utc_hour(dt.date(2026, 11, 19)) == 11  # EST, UTC-5


@pytest.mark.parametrize("now, cron, should_run", [
    (dt.datetime(2026, 10, 15, 10, 0, tzinfo=UTC), SUMMER_CRON, True),
    (dt.datetime(2026, 10, 15, 11, 0, tzinfo=UTC), WINTER_CRON, False),
    (dt.datetime(2026, 11, 19, 10, 0, tzinfo=UTC), SUMMER_CRON, False),
    (dt.datetime(2026, 11, 19, 11, 0, tzinfo=UTC), WINTER_CRON, True),
    # GitHub often starts scheduled runs late; the gate checks which cron
    # fired, not the clock, so a 2h47m delay still runs the right one.
    (dt.datetime(2026, 10, 15, 12, 47, tzinfo=UTC), SUMMER_CRON, True),
])
def test_exactly_one_cron_runs_each_thursday(now, cron, should_run):
    assert should_run_scheduled(cron, now)[0] is should_run


def test_an_unrecognised_cron_runs_rather_than_skipping_a_week():
    assert should_run_scheduled("", dt.datetime(2026, 10, 15, 10, tzinfo=UTC))[0] is True


# --- CLI ----------------------------------------------------------------------

@pytest.fixture
def scrape_ok(monkeypatch):
    monkeypatch.setattr(pipeline, "scrape_postal_code",
                        lambda pc: [current_row("BOK CHOY", "1.99"),
                                    current_row("PORK BELLY, 1 kg", "6.99")])


def test_off_dst_cron_exits_cleanly_without_touching_the_database(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    # Whichever cron is wrong for today's Toronto offset:
    today = dt.datetime.now(UTC).astimezone(weekly_refresh.TORONTO).date()
    wrong = WINTER_CRON if expected_utc_hour(today) == 10 else SUMMER_CRON
    monkeypatch.setenv("SCHEDULE", wrong)
    monkeypatch.setattr(migrate, "apply_all",
                        lambda: pytest.fail("a skipped run must not touch the database"))
    assert weekly_refresh.main([]) == 0


def test_ci_without_the_database_secret_fails_loudly(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("DATABASE_URL")
    assert weekly_refresh.main([]) == 1
    assert "DATABASE_URL repository secret is not set" in capsys.readouterr().out


def test_manual_ci_run_loads_reports_and_annotates(db, monkeypatch, scrape_ok, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("POSTAL_CODES", "m5s 1z9, V7C4K2")

    assert weekly_refresh.main([]) == 0
    batches = query(db, "SELECT postal_code, triggered_by FROM etl_batch ORDER BY 1")
    assert batches == [{"postal_code": "M5S1Z9", "triggered_by": "github_actions"},
                       {"postal_code": "V7C4K2", "triggered_by": "github_actions"}]
    report = summary.read_text()
    assert "| M5S1Z9 |" in report and "| V7C4K2 |" in report


def test_dq_failures_are_warnings_not_failures(db, monkeypatch, tmp_path, capsys):
    # 2 priced rows out of 10 scraped -> missing-price check fails.
    rows = [current_row("BOK CHOY", "1.99"), current_row("PORK BELLY", "6.99")]
    rows += [{**current_row(f"ITEM {i}", ""), "price": ""} for i in range(8)]
    monkeypatch.setattr(pipeline, "scrape_postal_code", lambda pc: rows)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))

    assert weekly_refresh.main(["M5S1Z9"]) == 0  # job stays green
    out = capsys.readouterr().out
    assert "::warning title=DQ M5S1Z9 missing_or_zero_price_pct::" in out
    assert "Failed data-quality checks" in (tmp_path / "s.md").read_text()


def test_one_failing_postal_code_does_not_stop_the_rest(db, monkeypatch):
    def flaky(pc):
        if pc == "V7C4K2":
            raise ConnectionError("Flipp timed out")
        return [current_row("BOK CHOY", "1.99")]

    monkeypatch.setattr(pipeline, "scrape_postal_code", flaky)
    results = weekly_refresh.refresh(["V7C4K2", "M5S1Z9"])
    assert [r["status"] for r in results] == ["error", "scraped"]
    assert weekly_refresh.main(["V7C4K2", "M5S1Z9"]) == 1  # load errors do fail the job


def test_defaults_to_active_postal_codes(db, monkeypatch, scrape_ok):
    import activity

    activity.record_request("M5S1Z9")
    results = weekly_refresh.refresh()
    assert [r["postal_code"] for r in results] == ["M5S1Z9"]


# --- the workflow file --------------------------------------------------------

def test_workflow_schedule_secret_and_manual_trigger():
    with open(WORKFLOW) as f:
        wf = yaml.safe_load(f)
    triggers = wf.get("on") or wf[True]  # PyYAML reads the bare key `on` as True
    assert sorted(c["cron"] for c in triggers["schedule"]) == [SUMMER_CRON, WINTER_CRON]
    assert "workflow_dispatch" in triggers
    assert "postal_codes" in triggers["workflow_dispatch"]["inputs"]

    step = next(s for s in wf["jobs"]["refresh"]["steps"]
                if s.get("run") == "python weekly_refresh.py")
    assert step["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
    assert step["env"]["SCHEDULE"] == "${{ github.event.schedule }}"
    assert wf["permissions"] == {"contents": "read"}
