from datetime import datetime

import pytest

from itemizer.cron import Cron


def nxt(expr, now):
    return Cron(expr).next_after(datetime.fromisoformat(now)).isoformat(sep=" ", timespec="minutes")


def test_daily_at_a_time():
    assert nxt("30 7 * * *", "2026-10-18 07:29") == "2026-10-18 07:30"
    assert nxt("30 7 * * *", "2026-10-18 07:30") == "2026-10-19 07:30"  # strictly after


def test_lists_ranges_and_steps():
    assert nxt("0 7,19 * * *", "2026-10-18 08:00") == "2026-10-18 19:00"
    assert nxt("*/15 * * * *", "2026-10-18 08:01") == "2026-10-18 08:15"
    assert nxt("0 9-17/4 * * *", "2026-10-18 10:00") == "2026-10-18 13:00"
    assert nxt("5/20 * * * *", "2026-10-18 08:06") == "2026-10-18 08:25"


def test_weekdays_and_names():
    # 2026-10-18 is a Sunday.
    assert nxt("0 8 * * mon-fri", "2026-10-18 09:00") == "2026-10-19 08:00"
    assert nxt("0 8 * * 0", "2026-10-18 07:00") == "2026-10-18 08:00"
    assert nxt("0 8 * * 7", "2026-10-18 09:00") == "2026-10-25 08:00"  # 7 is Sunday too
    assert nxt("0 0 1 jan *", "2026-10-18 00:00") == "2027-01-01 00:00"


def test_day_of_month_or_day_of_week_like_cron():
    # Both restricted: either one matches (the 1st, or any Monday).
    assert nxt("0 6 1 * mon", "2026-10-18 07:00") == "2026-10-19 06:00"
    assert nxt("0 6 1 * mon", "2026-10-31 07:00") == "2026-11-01 06:00"


def test_leap_day():
    assert nxt("0 0 29 2 *", "2026-10-18 00:00") == "2028-02-29 00:00"


@pytest.mark.parametrize("bad", ["", "30 7 * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *",
                                 "5-1 * * * *", "0 0 31 2 *"])
def test_bad_expressions_are_refused(bad):
    with pytest.raises(ValueError):
        Cron(bad).next_after(datetime(2026, 10, 18))


def test_config_refuses_a_bad_schedule(monkeypatch):
    from itemizer.config import Config
    monkeypatch.setenv("SURE_URL", "http://sure")
    monkeypatch.setenv("SURE_API_KEY", "k")
    monkeypatch.setenv("SCHEDULE", "every day")
    with pytest.raises(SystemExit, match="SCHEDULE"):
        Config.from_env()
    monkeypatch.setenv("SCHEDULE", "0  6 * * *")
    assert Config.from_env().schedule == "0 6 * * *"
