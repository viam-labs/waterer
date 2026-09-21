"""Tests for the pump component's pure-function helpers."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from waterer_module.pump import (
    _normalize_days,
    _normalize_schedule,
    _parse_hhmm,
)


# Mirror Pump._is_due as a free function so tests don't need to
# construct a Pump instance with a live Switch dependency.
def _is_due_helper(schedule, now_local, catch_up_window_min=30):
    """Mirrors Pump._is_due without needing a Pump instance."""
    try:
        hi, mi = _parse_hhmm(schedule.get("time"))
    except ValueError:
        return False
    dows = schedule.get("days_of_week") or []
    if dows and now_local.weekday() not in dows:
        return False
    fire_today = now_local.replace(hour=hi, minute=mi, second=0, microsecond=0)
    if now_local < fire_today:
        return False
    age_min = (now_local - fire_today).total_seconds() / 60
    if age_min > catch_up_window_min:
        return False
    last_iso = schedule.get("last_fired_at")
    if last_iso:
        try:
            last = datetime.fromisoformat(last_iso).astimezone(now_local.tzinfo)
            if last >= fire_today:
                return False
        except ValueError:
            pass
    return True


def test_normalize_days_accepts_int_valued_floats():
    """Viam sends numeric fields as doubles; ints must be accepted too."""
    assert _normalize_days([0.0, 1.0, 4.0]) == [0, 1, 4]
    assert _normalize_days([0, 1, 2, 3, 4]) == [0, 1, 2, 3, 4]


def test_normalize_days_rejects_bad_input():
    with pytest.raises(ValueError):
        _normalize_days([1.5])
    with pytest.raises(ValueError):
        _normalize_days([True])
    with pytest.raises(ValueError):
        _normalize_days([7])


def test_normalize_days_empty_and_none():
    assert _normalize_days(None) == []
    assert _normalize_days([]) == []


def test_normalize_schedule_populates_defaults():
    s = _normalize_schedule({"time": "07:30", "dose_ml": 250})
    assert s["time"] == "07:30"
    assert s["dose_ml"] == 250.0
    assert s["enabled"] is True
    assert s["days_of_week"] == []
    assert s["name"] == "Dispense 07:30"
    assert isinstance(s["id"], str) and len(s["id"]) == 8


def test_normalize_schedule_requires_dose_ml_positive():
    with pytest.raises(ValueError):
        _normalize_schedule({"time": "07:30", "dose_ml": 0})
    with pytest.raises(ValueError):
        _normalize_schedule({"time": "07:30"})


def test_normalize_schedule_requires_valid_time():
    with pytest.raises(ValueError):
        _normalize_schedule({"time": "25:00", "dose_ml": 100})
    with pytest.raises(ValueError):
        _normalize_schedule({"time": "nope", "dose_ml": 100})


def test_is_due_fires_only_once_per_day():
    tz = timezone(timedelta(hours=-5))
    now = datetime(2026, 9, 21, 7, 5, tzinfo=tz)
    fired = _is_due_helper(
        {"time": "07:00", "days_of_week": [], "last_fired_at": None},
        now,
    )
    assert fired is True

    # After firing at 07:03, another 07:05 tick should not re-fire.
    fired_again = _is_due_helper(
        {
            "time": "07:00",
            "days_of_week": [],
            "last_fired_at": now.replace(minute=3).astimezone(UTC).isoformat(),
        },
        now,
    )
    assert fired_again is False


def test_is_due_respects_days_of_week():
    tz = timezone(timedelta(hours=-5))
    # Sunday morning — weekday() == 6
    sunday = datetime(2026, 9, 20, 7, 5, tzinfo=tz)
    assert sunday.weekday() == 6
    weekdays_only = {"time": "07:00", "days_of_week": [0, 1, 2, 3, 4], "last_fired_at": None}
    assert _is_due_helper(weekdays_only, sunday) is False


def test_is_due_catch_up_window():
    tz = timezone(timedelta(hours=-5))
    # 45 min past a 07:00 schedule — outside the default 30-min catch-up.
    now = datetime(2026, 9, 21, 7, 45, tzinfo=tz)
    s = {"time": "07:00", "days_of_week": [], "last_fired_at": None}
    assert _is_due_helper(s, now) is False
