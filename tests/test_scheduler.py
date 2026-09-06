from datetime import datetime, time
import pytest


def check_schedule_due(scheduled_raw: str, current_time: time) -> bool:
    """Helper mirroring the scheduler's due check logic."""
    if ":" in scheduled_raw:
        try:
            s_time = datetime.strptime(scheduled_raw, "%H:%M").time()
            return s_time <= current_time
        except (ValueError, TypeError):
            return current_time.hour >= 9
    else:
        try:
            s_hour = int(scheduled_raw)
            return current_time.hour >= s_hour
        except (ValueError, TypeError):
            return current_time.hour >= 9


def test_schedule_due_exact_time():
    # 09:00 schedule evaluated at 09:00 -> due
    assert check_schedule_due("09:00", time(9, 0)) is True

    # 09:00 schedule evaluated at 08:59 -> not due
    assert check_schedule_due("09:00", time(8, 59)) is False

    # 09:00 schedule evaluated at 09:01 -> due
    assert check_schedule_due("09:00", time(9, 1)) is True

    # 18:30 schedule evaluated at 18:29 -> not due
    assert check_schedule_due("18:30", time(18, 29)) is False

    # 18:30 schedule evaluated at 18:30 -> due
    assert check_schedule_due("18:30", time(18, 30)) is True


def test_schedule_due_legacy_integer():
    # Legacy '9' hour format
    assert check_schedule_due("9", time(8, 59)) is False
    assert check_schedule_due("9", time(9, 0)) is True
    assert check_schedule_due("9", time(10, 30)) is True


def test_schedule_fallback_on_corrupt_data():
    # Corrupt string falls back safely to 9:00
    assert check_schedule_due("invalid_time", time(8, 30)) is False
    assert check_schedule_due("invalid_time", time(9, 0)) is True
