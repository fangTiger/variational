"""Swap 交易时段解析测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.swap_trading_schedule import parse_trading_schedule


SESSIONS = [
    {"open": "2026-09-02T22:00:00Z", "close": "2026-09-03T21:00:00Z"},
    {"open": "2026-09-03T22:00:00Z", "close": "2026-09-04T21:00:00Z"},
    {"open": "2026-09-06T22:00:00Z", "close": "2026-09-07T18:30:00Z"},
]
SCHEDULE = {
    "next_open_at": "2026-09-06T22:00:00Z",
    "next_close_at": "2026-09-04T21:00:00Z",
}


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    """构造 2026 年 9 月的 UTC 时间。"""
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_open_session_reports_actual_close_and_remaining_time() -> None:
    """时段内应采用会话里的实际休市点。"""
    result = parse_trading_schedule(
        trading_sessions=SESSIONS,
        trading_schedule=SCHEDULE,
        market_status="open",
        now=_utc(4, 20, 30),
    )

    assert result.is_tradable is True
    assert result.next_close_at == _utc(4, 21)
    assert result.next_open_at == _utc(6, 22)
    assert result.time_until_close == timedelta(minutes=30)
    assert result.closure_duration == timedelta(hours=49)
    assert result.metadata_is_fresh is True


def test_weekend_close_reports_full_49_hour_closure() -> None:
    """周末中途观察仍报告完整休市窗口，不是仅报告剩余时间。"""
    result = parse_trading_schedule(
        trading_sessions=SESSIONS,
        trading_schedule=SCHEDULE,
        market_status="closed",
        now=_utc(5, 12),
    )

    assert result.is_tradable is False
    assert result.next_open_at == _utc(6, 22)
    assert result.next_close_at == _utc(7, 18, 30)
    assert result.time_until_close is None
    assert result.closure_duration == timedelta(hours=49)
    assert result.metadata_is_fresh is True


def test_empty_metadata_fails_closed() -> None:
    """空元数据不得默认可交易。"""
    result = parse_trading_schedule(
        trading_sessions=[],
        trading_schedule={},
        market_status="open",
        now=_utc(4, 20),
    )

    assert result.is_tradable is False
    assert result.metadata_is_fresh is False
    assert result.next_close_at is None
    assert result.next_open_at is None
    assert result.time_until_close is None
    assert result.closure_duration is None
    assert "缺失" in result.reason


def test_malformed_session_fails_closed() -> None:
    """无法解析任一会话时保守判为不可交易。"""
    result = parse_trading_schedule(
        trading_sessions=[{"open": "不是时间", "close": "2026-09-04T21:00:00Z"}],
        trading_schedule=SCHEDULE,
        market_status="open",
        now=_utc(4, 20),
    )

    assert result.is_tradable is False
    assert result.metadata_is_fresh is False
    assert "解析" in result.reason


def test_sessions_ending_before_now_are_stale_and_fail_closed() -> None:
    """最新会话已结束且没有未来会话时，市场状态 open 也不可采信。"""
    result = parse_trading_schedule(
        trading_sessions=SESSIONS[:2],
        trading_schedule=SCHEDULE,
        market_status="open",
        now=_utc(5, 12),
    )

    assert result.is_tradable is False
    assert result.metadata_is_fresh is False
    assert "陈旧" in result.reason


def test_holiday_early_close_comes_from_session_not_fixed_clock() -> None:
    """劳动节 18:30Z 提前收市必须覆盖常规 21:00Z 假设。"""
    result = parse_trading_schedule(
        trading_sessions=SESSIONS,
        trading_schedule={
            "next_open_at": "2026-09-07T22:00:00Z",
            "next_close_at": "2026-09-07T18:30:00Z",
        },
        market_status="open",
        now=_utc(7, 18),
    )

    assert result.is_tradable is True
    assert result.next_close_at == _utc(7, 18, 30)
    assert result.time_until_close == timedelta(minutes=30)


def test_schedule_can_describe_closure_longer_than_49_hours() -> None:
    """节假日长休市采用元数据实际值，不封顶为 49 小时。"""
    sessions = [
        {"open": "2026-09-03T22:00:00Z", "close": "2026-09-04T18:30:00Z"},
        {"open": "2026-09-07T22:00:00Z", "close": "2026-09-08T21:00:00Z"},
    ]
    schedule = {
        "next_open_at": "2026-09-07T22:00:00Z",
        "next_close_at": "2026-09-08T21:00:00Z",
    }

    result = parse_trading_schedule(
        trading_sessions=sessions,
        trading_schedule=schedule,
        market_status="closed",
        now=_utc(5, 12),
    )

    assert result.is_tradable is False
    assert result.next_open_at == _utc(7, 22)
    assert result.next_close_at == _utc(8, 21)
    assert result.closure_duration == timedelta(hours=75, minutes=30)
    assert result.closure_duration > timedelta(hours=49)
