"""Swap carry provider：只读采集 XAUS 多腿与 XAU 空腿状态。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from panel.types import Metric, PanelAlert, SystemStatus
from tools import hedge_swap_carry as carry
from tools import run_swap_carry_guard as guard


NAME = "Swap Carry（XAUS/XAU）"


def _leg_value(size: Decimal, notional: Decimal | None) -> str:
    """格式化单腿数量与绝对名义。"""
    value = f"{size:+.5f}"
    if notional is not None:
        value += f" / ${notional:,.2f}"
    else:
        value += " / 无数据"
    return value


def _carry_tone(value: Decimal | None) -> str:
    """按策略最低可接受收益给净 carry 着色。"""
    if value is None:
        return "normal"
    if value >= Decimal("0.05"):
        return "good"
    if value >= 0:
        return "warn"
    return "bad"


def _format_datetime(value: datetime | None) -> str:
    """把时段边界统一显示为 UTC。"""
    if value is None:
        return "无数据"
    return value.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")


def _read_heartbeat(
    path: Path,
    observed_at: datetime,
) -> tuple[bool | None, Metric, PanelAlert | None]:
    """读取守护进程心跳；缺失只降级，陈旧必须报警。"""
    heartbeat = carry._read_guard_json(path)
    timestamp = (
        carry._guard_timestamp(heartbeat.get("timestamp"))
        if heartbeat is not None
        else None
    )
    if timestamp is None:
        return None, Metric("守护进程心跳", "无数据", "normal"), None

    age = max(timedelta(0), observed_at - timestamp)
    age_minutes = age.total_seconds() / 60
    stale = age > carry.SWAP_CARRY_GUARD_STALE_AFTER
    metric = Metric(
        "守护进程心跳",
        f"上次运行于 {age_minutes:.1f} 分钟前",
        "bad" if stale else "good",
    )
    if not stale:
        return True, metric, None
    return (
        False,
        metric,
        PanelAlert(
            key="swap_carry_heartbeat_stale",
            level="critical",
            title=f"Swap carry 守护进程已 {age_minutes:.1f} 分钟未运行",
            action="立即检查并重启守护进程；重启前人工核对两腿仓位与强平风险",
        ),
    )


def _guard_alert(path: Path) -> PanelAlert | None:
    """把守护状态中的地区封锁或会话失效升级为严重告警。"""
    state = carry._read_guard_json(path)
    if state is None:
        return None
    message = str(state.get("message") or "")
    if not any(marker in message for marker in ("地区封锁", "会话失效")):
        return None
    return PanelAlert(
        key="swap_carry_guard_blocked",
        level="critical",
        title=f"Swap carry 守护进程受阻：{message}",
        action="立即切换到放行网络或更新登录会话，并人工确认两腿仍然对冲",
    )


async def _liquidation_metric(
    client: Any,
    *,
    label: str,
    underlying: str,
    position: Any,
) -> tuple[Metric, Decimal | None]:
    """独立读取一腿权威强平价，失败不影响其他指标。"""
    if position.is_flat:
        return Metric(label, "无持仓"), None
    try:
        info = await client.get_liquidation_info(underlying, exact=True)
        text = carry._format_liquidation(info, position)
        distance = guard._liquidation_distance(info, position)
    except Exception:  # noqa: BLE001 单项失败只降级这一行
        return Metric(label, "无数据"), None
    tone = "bad" if distance < guard.LIQUIDATION_ALERT_RATIO else "good"
    return Metric(label, text, tone), distance


def _schedule_metric(schedule: Any | None) -> Metric:
    """格式化 XAUS 当前时段和下一次边界。"""
    if schedule is None:
        return Metric(
            "XAUS 时段",
            "不可交易（数据不可用）；距下次休市=无数据；下次开市=无数据",
            "warn",
        )
    status = "可交易" if schedule.is_tradable else "不可交易"
    value = (
        f"{status}；距下次休市={carry._format_duration(schedule.time_until_close)}；"
        f"下次开市={_format_datetime(schedule.next_open_at)}"
    )
    return Metric("XAUS 时段", value, "good" if schedule.is_tradable else "warn")


def _week_start(observed_at: datetime) -> datetime:
    """返回观察时点所在 UTC 周的周一零点。"""
    midnight = observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def _funding_value(settled: Mapping[str, Decimal]) -> str:
    """格式化两腿本周实际结算扣款。"""
    xaus = settled[carry.XAUS_LEG.underlying]
    xau = settled[carry.XAU_LEG.underlying]
    total = xaus + xau
    return f"XAUS {xaus:+.2f} / XAU {xau:+.2f} / 合计 {total:+.2f} USDC"


async def _collect(
    client: Any,
    *,
    heartbeat_path: Path,
    state_path: Path,
    observed_at: datetime,
) -> SystemStatus:
    """执行一轮只读采集；账户仓位是卡片成立所需的核心数据。"""
    xaus_position = await carry._get_position(client, carry.XAUS_LEG)
    xau_position = await carry._get_position(client, carry.XAU_LEG)

    alive, heartbeat_metric, heartbeat_alert = _read_heartbeat(
        heartbeat_path,
        observed_at,
    )
    alerts = [alert for alert in (heartbeat_alert, _guard_alert(state_path)) if alert]

    single_leg = xaus_position.is_flat != xau_position.is_flat
    if single_leg:
        remaining = "XAU" if xaus_position.is_flat else "XAUS"
        alerts.append(
            PanelAlert(
                key="swap_carry_single_leg",
                level="critical",
                title=f"Swap carry 单腿裸仓：只剩 {remaining}",
                action="停止新增仓位并立即人工恢复对冲或安全减掉剩余单腿",
            )
        )

    metadata: object | None = None
    schedule = None
    try:
        metadata, _record, schedule = await carry._load_schedule(
            client,
            now=observed_at,
        )
    except Exception:  # noqa: BLE001 时段和名义独立降级
        pass

    xaus_price = (
        carry._metadata_price(metadata, carry.XAUS_LEG)
        if metadata is not None
        else None
    )
    xau_price = (
        carry._metadata_price(metadata, carry.XAU_LEG)
        if metadata is not None
        else None
    )
    xaus_notional = (
        abs(xaus_position.signed_size) * xaus_price
        if xaus_price is not None
        else None
    )
    xau_notional = (
        abs(xau_position.signed_size) * xau_price
        if xau_price is not None
        else None
    )

    net_delta = xaus_position.signed_size + xau_position.signed_size
    net_tone = "good" if abs(net_delta) <= carry.XAUS_QTY_STEP else "bad"

    xaus_liquidation, xaus_distance = await _liquidation_metric(
        client,
        label="XAUS 强平",
        underlying=carry.XAUS_LEG.underlying,
        position=xaus_position,
    )
    xau_liquidation, _xau_distance = await _liquidation_metric(
        client,
        label="XAU 强平",
        underlying=carry.XAU_LEG.underlying,
        position=xau_position,
    )
    if (
        xaus_distance is not None
        and xaus_distance < guard.LIQUIDATION_ALERT_RATIO
    ):
        alerts.append(
            PanelAlert(
                key="swap_carry_xaus_liquidation",
                level="critical",
                title=f"XAUS 强平距离仅 {xaus_distance:.2%}",
                action="立即人工核对保证金，并优先安全减小 XAUS 与 XAU 两腿仓位",
            )
        )

    xaus_rate: Decimal | None = None
    xau_rate: Decimal | None = None
    try:
        xaus_rate = carry._swap_long_rate(
            await client.get_swap_funding(carry.XAUS_LEG.underlying)
        )
    except Exception:  # noqa: BLE001 单项失败后仍继续读取 XAU
        pass
    try:
        xau_rate = carry._decimal(
            await client.get_funding_rate(
                carry.XAU_LEG.underlying,
                carry.XAU_LEG.instrument_type,
            ),
            label="XAU 永续资金费率",
        )
    except Exception:  # noqa: BLE001 单项失败只令净 carry 无数据
        pass
    net_carry = (
        xaus_rate + xau_rate
        if xaus_rate is not None and xau_rate is not None
        else None
    )
    if net_carry is not None and net_carry < 0:
        alerts.append(
            PanelAlert(
                key="swap_carry_negative_carry",
                level="warning",
                title=f"Swap carry 已转负至 {net_carry:.1%}/年",
                action="停止加仓并人工复核两腿资金费；确认持续为负后择机平掉双腿",
            )
        )

    try:
        settled = await carry._settled_funding_by_leg(
            client,
            since=_week_start(observed_at),
        )
        funding_metric = Metric("本周已结算资金费", _funding_value(settled))
    except Exception:  # noqa: BLE001 资金费失败不遮蔽仓位与风险
        funding_metric = Metric("本周已结算资金费", "无数据")

    if net_carry is None:
        carry_value = "无数据"
    else:
        carry_value = f"{net_carry:+.1%}"

    metrics = [
        Metric("XAUS 多腿", _leg_value(xaus_position.signed_size, xaus_notional)),
        Metric("XAU 空腿", _leg_value(xau_position.signed_size, xau_notional)),
        Metric("净 delta", f"{net_delta:+.5f}", net_tone),
        Metric("净 carry 年化", carry_value, _carry_tone(net_carry)),
        xaus_liquidation,
        xau_liquidation,
        _schedule_metric(schedule),
        heartbeat_metric,
        funding_metric,
    ]

    if single_leg:
        summary = "单腿持仓，需立即处理"
    elif xaus_position.is_flat and xau_position.is_flat:
        summary = "空仓，可交易" if schedule is not None and schedule.is_tradable else "空仓，等待开市"
    elif net_carry is None:
        summary = "持仓中，净 carry 无数据"
    else:
        summary = f"持仓中，净 carry {net_carry:+.1%}/年"

    return SystemStatus(
        name=NAME,
        alive=alive,
        summary=summary,
        metrics=metrics,
        alerts=alerts,
    )


def collect(
    *,
    client: Any | None = None,
    heartbeat_path: Path = carry.SWAP_CARRY_GUARD_HEARTBEAT,
    state_path: Path = carry.SWAP_CARRY_GUARD_STATE,
    now: datetime | None = None,
) -> SystemStatus:
    """同步 provider 入口；任何整轮失败都返回 error 卡片。"""
    observed_at = now or datetime.now(timezone.utc)
    owned_client = client is None

    async def run() -> SystemStatus:
        nonlocal client
        carry.remove_proxy_environment(os.environ)
        if client is None:
            client = await carry._load()
        try:
            return await _collect(
                client,
                heartbeat_path=heartbeat_path,
                state_path=state_path,
                observed_at=observed_at.astimezone(timezone.utc),
            )
        finally:
            if owned_client and client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 关闭失败不能覆盖采集结果
                    pass

    try:
        if observed_at.tzinfo is None:
            raise ValueError("now 必须包含时区")
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 单个 provider 不能拖垮整页
        return SystemStatus(
            name=NAME,
            alive=None,
            summary="采集失败",
            error=str(exc),
        )
