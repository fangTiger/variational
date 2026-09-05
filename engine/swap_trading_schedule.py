"""基于交易所元数据的 swap 交易时段解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SwapTradingSchedule:
    """某一观察时点的保守交易时段判断。"""

    is_tradable: bool
    next_close_at: datetime | None
    next_open_at: datetime | None
    time_until_close: timedelta | None
    closure_duration: timedelta | None
    metadata_is_fresh: bool
    reason: str


@dataclass(frozen=True)
class TradingSession:
    """已解析并统一为 UTC 的一个交易会话。"""

    open_at: datetime
    close_at: datetime


def _closed_result(reason: str) -> SwapTradingSchedule:
    """构造元数据不可用时的失败关闭结果。"""
    return SwapTradingSchedule(
        is_tradable=False,
        next_close_at=None,
        next_open_at=None,
        time_until_close=None,
        closure_duration=None,
        metadata_is_fresh=False,
        reason=reason,
    )


def parse_utc_timestamp(value: Any, *, label: str) -> datetime:
    """解析带时区 ISO8601 时间并统一为 UTC。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 缺失")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} 无法解析") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 缺少时区")
    return parsed.astimezone(timezone.utc)


def _parse_schedule_time(
    schedule: Mapping[str, Any], field: str
) -> datetime | None:
    """解析可缺省的 schedule 时间；字段存在但非法时仍失败。"""
    value = schedule.get(field)
    if value in (None, ""):
        return None
    return parse_utc_timestamp(value, label=f"trading_schedule.{field}")


def parse_sessions(
    trading_sessions: Sequence[Mapping[str, Any]] | None,
) -> tuple[TradingSession, ...]:
    """严格解析并按开市时间排序交易会话。"""
    if not trading_sessions:
        raise ValueError("trading_sessions 元数据缺失")
    sessions: list[TradingSession] = []
    for index, item in enumerate(trading_sessions):
        if not isinstance(item, Mapping):
            raise ValueError(f"trading_sessions[{index}] 不是对象")
        open_at = parse_utc_timestamp(
            item.get("open"), label=f"trading_sessions[{index}].open"
        )
        close_at = parse_utc_timestamp(
            item.get("close"), label=f"trading_sessions[{index}].close"
        )
        if open_at >= close_at:
            raise ValueError(f"trading_sessions[{index}] 开市时间不早于收市时间")
        sessions.append(TradingSession(open_at, close_at))

    sessions.sort(key=lambda session: session.open_at)
    if any(
        previous.close_at > current.open_at
        for previous, current in zip(sessions, sessions[1:])
    ):
        raise ValueError("trading_sessions 会话发生重叠")
    return tuple(sessions)


def parse_trading_schedule(
    trading_sessions: Sequence[Mapping[str, Any]] | None,
    trading_schedule: Mapping[str, Any] | None,
    market_status: str | None,
    now: datetime,
) -> SwapTradingSchedule:
    """按真实会话边界解析当前状态、下次边界及完整休市时长。

    ``trading_sessions`` 决定当前是否位于交易区间和实际收市时间；
    ``trading_schedule`` 补充交易所声明的下一开、收市时间。任何缺失、
    无法解析或陈旧状态都失败关闭，不采用固定时钟或固定 49 小时假设。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    observed_at = now.astimezone(timezone.utc)
    if not trading_sessions:
        return _closed_result("trading_sessions 元数据缺失")
    if not isinstance(trading_schedule, Mapping):
        return _closed_result("trading_schedule 元数据缺失")

    try:
        sessions = parse_sessions(trading_sessions)
        schedule_open = _parse_schedule_time(trading_schedule, "next_open_at")
        schedule_close = _parse_schedule_time(trading_schedule, "next_close_at")
    except (TypeError, ValueError) as exc:
        return _closed_result(f"交易时段元数据无法解析：{exc}")

    latest_close = max(session.close_at for session in sessions)
    if latest_close <= observed_at:
        return _closed_result("trading_sessions 元数据陈旧：最新会话已经结束")

    active = next(
        (
            session
            for session in sessions
            if session.open_at <= observed_at < session.close_at
        ),
        None,
    )
    previous_closes = [
        session.close_at for session in sessions if session.close_at <= observed_at
    ]
    future_sessions = [
        session for session in sessions if session.open_at > observed_at
    ]

    if active is not None:
        next_close_at = active.close_at
        future_opens = [
            session.open_at
            for session in future_sessions
            if session.open_at >= active.close_at
        ]
        if schedule_open is not None and schedule_open >= active.close_at:
            next_open_at = schedule_open
        else:
            next_open_at = min(future_opens, default=None)
        time_until_close = next_close_at - observed_at
        closure_duration = (
            next_open_at - next_close_at if next_open_at is not None else None
        )
    else:
        future_opens = [session.open_at for session in future_sessions]
        if schedule_open is not None and schedule_open > observed_at:
            next_open_at = schedule_open
        else:
            next_open_at = min(future_opens, default=None)

        future_closes = [
            session.close_at
            for session in future_sessions
            if next_open_at is None or session.open_at >= next_open_at
        ]
        if schedule_close is not None and schedule_close > observed_at:
            next_close_at = schedule_close
        else:
            next_close_at = min(future_closes, default=None)
        time_until_close = None
        previous_close = max(previous_closes, default=None)
        closure_duration = (
            next_open_at - previous_close
            if next_open_at is not None and previous_close is not None
            else None
        )

    status_open = str(market_status or "").strip().lower() == "open"
    is_tradable = status_open and active is not None
    if is_tradable:
        reason = "当前位于交易会话内且 market_status 为 open"
    elif not status_open:
        reason = f"market_status={market_status!r}，当前不可交易"
    else:
        reason = "market_status 为 open，但当前时间不在任何交易会话内"

    return SwapTradingSchedule(
        is_tradable=is_tradable,
        next_close_at=next_close_at,
        next_open_at=next_open_at,
        time_until_close=time_until_close,
        closure_duration=closure_duration,
        metadata_is_fresh=True,
        reason=reason,
    )
