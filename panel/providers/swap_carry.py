"""Swap carry provider：按当前具名结构只读采集多腿状态。"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Mapping

from adapters.base import Position
from panel.types import Metric, PanelAlert, SystemStatus
from tools import hedge_swap_carry as carry
from tools import run_swap_carry_guard as guard
from tools.show_switch_history import load_switch_history


from engine import swap_carry_cost as cost


NAME = "Swap Carry（XAUS/XAU）"
UNKNOWN_STRUCTURE = "结构未知（守护心跳不可用）"
IGNORED_EXTERNAL_POSITIONS = frozenset({"BTC"})


def _leg_value(
    size: Decimal,
    notional: Decimal | None,
    weight: Decimal,
    unrealized_pnl: Decimal | None,
) -> str:
    """格式化单腿权重、数量、绝对名义与未实现盈亏。"""
    value = f"权重={weight} / {size:+.5f}"
    if notional is not None:
        value += f" / ${notional:,.2f}"
    else:
        value += " / 无数据"
    if unrealized_pnl is not None:
        value += f" / 未实现盈亏={unrealized_pnl:+.2f} USDC"
    else:
        value += " / 未实现盈亏=无数据"
    return value


def _position_upnl(position: Position) -> Decimal | None:
    """从已读取的原始持仓中提取未实现盈亏，不额外请求接口。"""
    raw = position.raw
    if not isinstance(raw, Mapping) or raw.get("upnl") in (None, ""):
        return None
    try:
        return carry._decimal(raw["upnl"], label=f"{position.market} 未实现盈亏")
    except ValueError:
        return None


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
        f"{_format_datetime(timestamp)}（{age_minutes:.1f} 分钟前）",
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


def _heartbeat_structure(path: Path) -> carry.CarryStructure | None:
    """只接受守护心跳声明的合法结构，缺失或非法时保持未知。"""
    heartbeat = carry._read_guard_json(path)
    raw_structure = heartbeat.get("structure") if heartbeat is not None else None
    if not isinstance(raw_structure, str) or not raw_structure.strip():
        return None
    try:
        return carry.resolve_structure(raw_structure)
    except ValueError:
        return None


def _position_items(payload: object) -> Sequence[object]:
    """兼容 `/positions` 的列表响应与带 positions 键的对象响应。"""
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("positions")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            return items
    raise ValueError("/positions 响应缺少持仓列表")


def _actual_positions(payload: object) -> list[Position]:
    """按 `/positions` 原始顺序提取全部标的和有符号数量。"""
    positions: list[Position] = []
    for raw_position in _position_items(payload):
        if not isinstance(raw_position, Mapping):
            raise ValueError("/positions 持仓记录不是对象")
        raw_info = raw_position.get("position_info", raw_position)
        if not isinstance(raw_info, Mapping):
            raise ValueError("/positions.position_info 不是对象")
        raw_instrument = raw_info.get("instrument")
        instrument = raw_instrument if isinstance(raw_instrument, Mapping) else {}
        underlying_value = instrument.get("underlying") or raw_info.get("underlying")
        underlying = str(underlying_value or "").strip().upper()
        if not underlying:
            raise ValueError("/positions 持仓缺少 instrument.underlying")
        qty = carry._decimal(
            raw_info.get("qty", raw_info.get("size")),
            label=f"{underlying} 持仓数量",
        )
        positions.append(Position(underlying, qty, raw=raw_position))
    return positions


def _structure_positions(
    selected: carry.CarryStructure,
    actual_positions: Sequence[Position],
) -> dict[str, Position]:
    """从全账户快照提取当前结构各腿，缺席腿按空仓处理。"""
    positions = {
        leg.underlying: Position(leg.underlying, Decimal("0"))
        for leg in selected.legs
    }
    seen: set[str] = set()
    for position in actual_positions:
        if position.market not in positions:
            continue
        if position.market in seen:
            raise ValueError(f"/positions 返回重复的结构腿 {position.market}")
        seen.add(position.market)
        positions[position.market] = position
    return positions


def _actual_position_metrics(positions: Sequence[Position]) -> list[Metric]:
    """把账户快照逐条展示；空列表也明确显示为空仓。"""
    if not positions:
        return [Metric("实际持仓", "无持仓")]
    return [
        Metric(f"实际持仓 {position.market}", f"{position.signed_size:+.5f}")
        for position in positions
    ]


def _unknown_structure_status(
    *,
    actual_positions: Sequence[Position],
    alive: bool | None,
    heartbeat_metric: Metric,
    session_metric: Metric,
    last_switch_metric: Metric,
    next_switch_metric: Metric,
    alerts: list[PanelAlert],
) -> SystemStatus:
    """结构不可确认时只展示事实，不猜测腿方向或裸仓状态。"""
    alerts.append(
        PanelAlert(
            key="swap_carry_structure_unknown",
            level="warning",
            title=UNKNOWN_STRUCTURE,
            action="检查守护进程是否在运行，并确认心跳已写入合法 structure",
        )
    )
    return SystemStatus(
        name=NAME,
        alive=alive,
        summary="结构未知，需人工核对持仓",
        metrics=[
            Metric("当前结构", UNKNOWN_STRUCTURE, "warn"),
            *_actual_position_metrics(actual_positions),
            session_metric,
            heartbeat_metric,
            last_switch_metric,
            next_switch_metric,
        ],
        alerts=alerts,
    )


def _session_expiry_metric(path: Path) -> tuple[Metric, PanelAlert | None]:
    """从守护心跳读取会话剩余时间，不接触 Cookie。"""
    heartbeat = carry._read_guard_json(path)
    raw_hours = heartbeat.get("session_hours_left") if heartbeat else None
    try:
        if isinstance(raw_hours, bool):
            raise ValueError
        hours_left = float(raw_hours)
        if not math.isfinite(hours_left):
            raise ValueError
    except (TypeError, ValueError):
        return Metric("会话剩余", "无数据"), None

    if hours_left > 48:
        tone = "good"
    elif hours_left >= 24:
        tone = "normal"
    elif hours_left >= 6:
        tone = "warn"
    else:
        tone = "bad"

    value = (
        f"{hours_left:.1f} 小时"
        if hours_left >= 0
        else f"已过期 {abs(hours_left):.1f} 小时"
    )
    metric = Metric("会话剩余", value, tone)
    if hours_left >= 6:
        return metric, None

    title = (
        f"Variational 会话仅剩 {hours_left:.1f} 小时"
        if hours_left >= 0
        else f"Variational 会话已过期 {abs(hours_left):.1f} 小时"
    )
    return (
        metric,
        PanelAlert(
            key="swap_carry_session_expiry",
            level="critical",
            title=title,
            action="按 docs/guides/导出-Variational-会话Cookie.md 重新导出",
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


def _switch_incident_alert(
    heartbeat_path: Path,
    state_path: Path,
) -> PanelAlert | None:
    """任一持久化来源出现切换事故时升级为最高级告警。"""
    heartbeat = carry._read_guard_json(heartbeat_path)
    state = carry._read_guard_json(state_path)
    if not any(
        payload is not None and payload.get("switch_incident") is True
        for payload in (heartbeat, state)
    ):
        return None
    return PanelAlert(
        key="swap_carry_switch_incident",
        level="critical",
        title="Swap carry 切换后自检失败，自动开仓与切换已锁定",
        action=(
            "立即人工核对台账与全部账户持仓；确认风险解除后，"
            "再人工清除 switch_incident 标记"
        ),
    )


def _rehearsal_metric(path: Path) -> tuple[Metric, PanelAlert | None]:
    """无论持仓采集是否成功，都从心跳展示预演及阻断告警。"""
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        report = heartbeat.get("last_rehearsal")
        if not isinstance(report, Mapping):
            return Metric("上次预演", "尚无预演"), None
        conclusion = report.get("conclusion", "未知")
        blocked = conclusion == "blocked" or heartbeat.get("rehearsal_blocked") is True
        reasons = "；".join(report.get("blocking_reasons", []))
        value = f"{report.get('timestamp', '时间未知')} / {conclusion}"
        if reasons:
            value += f" / {reasons}"
        alert = PanelAlert(
            key="swap_carry_rehearsal_blocked", level="critical",
            title=f"结构切换预演 blocked：{reasons}",
            action="本窗口禁止切换；平仓风控仍生效，请检查预演台账",
        ) if blocked else None
        return Metric("上次预演", value, "bad" if blocked else "warn" if conclusion == "warning" else "good"), alert
    except (OSError, ValueError, TypeError, AttributeError):
        return Metric("上次预演", "无数据", "warn"), None


def _last_switch_metric(path: Path) -> Metric:
    """读取最近一条切换台账；缺失与损坏采用不同降级文案。"""
    records, error = load_switch_history(path)
    records = [record for record in records if record.get("kind") != "rehearsal"]
    if error is not None:
        return Metric("上次切换", "台账不可用（文件损坏）", "warn")
    if not records:
        return Metric("上次切换", "尚无切换")

    record = records[-1]
    started_at = record.get("started_at")
    timestamp = "时间未知"
    if isinstance(started_at, str):
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                timestamp = parsed.astimezone(timezone.utc).strftime(
                    "%m-%d %H:%M UTC"
                )

    direction = record.get("direction")
    if isinstance(direction, Mapping):
        source = str(direction.get("from") or "?")
        target = str(direction.get("to") or "?")
        direction_text = f"{source}→{target}"
    else:
        direction_text = "方向未知"
    wear = record.get("measured_wear_usd")
    wear_text = str(wear) if wear is not None else "无数据"
    return Metric(
        "上次切换",
        f"{timestamp} / {direction_text} / 实测磨损 {wear_text} USDC",
    )


def _next_switch_metric(schedule: Any | None) -> Metric:
    """按权威 XAUS 会话边界推算下一次长休市结构切换。"""
    if (
        schedule is None
        or not schedule.metadata_is_fresh
        or schedule.closure_duration is None
        or schedule.closure_duration <= guard.LONG_CLOSURE_THRESHOLD
    ):
        return Metric("下次切换预计", "暂无可推算的长休市切换")

    if schedule.is_tradable and schedule.next_close_at is not None:
        switch_at = schedule.next_close_at - guard.SWITCH_LEAD_TIME
        direction = "XAUS_XAU→XAU_XAUT"
    elif not schedule.is_tradable and schedule.next_open_at is not None:
        switch_at = schedule.next_open_at
        direction = "XAU_XAUT→XAUS_XAU"
    else:
        return Metric("下次切换预计", "暂无可推算的长休市切换")
    return Metric(
        "下次切换预计",
        f"{_format_datetime(switch_at)} / {direction}",
    )


def _selection_metrics(path: Path, current: str) -> list[Metric]:
    """展示守护轮次的最优结构，并解释尚未采用该结构的原因。"""
    saved = {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        best = saved.get("best_structure") or "无可用候选"
        decision = saved.get("selection_decision") or {}
        reason = decision.get("reason") or "尚无结构择优记录"
        action_reason = saved.get("auto_switch_conclusion")
        if current == "空仓":
            action_reason = saved.get("auto_open_conclusion")
        if action_reason and action_reason != reason:
            reason += "；" + action_reason
    except (OSError, ValueError, TypeError, AttributeError):
        best, reason = "无数据", "无法读取守护进程的结构择优记录"
    metrics = [Metric("最优结构", best)]
    try:
        candidates = saved.get("candidate_structures") or {}
        current_points = candidates.get(current, {}).get("points_oi")
        best_points = candidates.get(best, {}).get("points_oi")
        current_text = f"{Decimal(current_points):,.0f}" if current_points is not None else "无数据"
        best_text = f"{Decimal(best_points):,.0f}" if best_points is not None else "无数据"
        metrics.append(Metric("积分 OI（同规模）", f"当前结构 {current_text} / 最优结构 {best_text}"))
        actual = saved.get("current_points_oi")
        if actual is not None:
            metrics.append(Metric("当前持仓积分 OI", f"{Decimal(actual):,.0f}"))
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        metrics.append(Metric("积分 OI（同规模）", "无数据"))
    if best != current:
        metrics.append(Metric("结构选择依据", reason, "warn"))
    return metrics


async def _allocation_metric(client: Any, underlying: str) -> Metric:
    """只读展示隔离腿当前桶、目标总额及强平距离，失败独立降级。"""
    try:
        allocation = await client.get_isolated_allocation(underlying)
        target = guard.required_allocation(
            allocation["notional"], allocation["maintenance_margin"],
            Decimal(os.environ.get("TARGET_LIQUIDATION_DISTANCE", str(guard.TARGET_LIQUIDATION_DISTANCE))),
        ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return Metric(
            f"{underlying} 隔离保证金",
            f"当前桶 ${guard._actual_allocation(allocation):,.2f} / 目标桶 ${target:,.2f} / 距离 {allocation['distance']:.2%}"
            f" / IM 公式要求值 ${allocation['initial_margin']:,.2f}（不含 allocation 追加部分）",
        )
    except Exception:  # noqa: BLE001 保证金读数失败不阻断其他面板指标
        return Metric(f"{underlying} 隔离保证金", "当前桶 / 目标桶 / 距离：无数据")


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
        distance = guard._liquidation_distance(
            info,
            position,
            underlying=underlying,
        )
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


def _funding_value(
    settled: Mapping[str, Decimal],
    structure: carry.CarryStructure | str,
) -> str:
    """按结构顺序格式化本周各腿实际结算扣款。"""
    selected = carry.resolve_structure(structure)
    total = sum(
        (settled[leg.underlying] for leg in selected.legs),
        Decimal("0"),
    )
    details = " / ".join(
        f"{leg.underlying} {settled[leg.underlying]:+.2f}"
        for leg in selected.legs
    )
    return f"{details} / 合计 {total:+.2f} USDC"


async def _collect(
    client: Any,
    *,
    heartbeat_path: Path,
    state_path: Path,
    switch_history_path: Path,
    observed_at: datetime,
) -> SystemStatus:
    """执行一轮只读采集；账户仓位是卡片成立所需的核心数据。"""
    alive, heartbeat_metric, heartbeat_alert = _read_heartbeat(
        heartbeat_path,
        observed_at,
    )
    session_metric, session_alert = _session_expiry_metric(heartbeat_path)
    alerts = [
        alert
        for alert in (
            heartbeat_alert,
            session_alert,
            _guard_alert(state_path),
            _switch_incident_alert(heartbeat_path, state_path),
        )
        if alert
    ]
    last_switch_metric = _last_switch_metric(switch_history_path)
    actual_positions = _actual_positions(await client.get_positions())
    metadata: object | None = None
    schedule = None
    try:
        metadata, _record, schedule = await carry._load_schedule(
            client,
            now=observed_at,
        )
    except Exception:  # noqa: BLE001 时段、名义和预计切换独立降级
        pass
    next_switch_metric = _next_switch_metric(schedule)
    selected = _heartbeat_structure(heartbeat_path)
    if selected is None:
        return _unknown_structure_status(
            actual_positions=actual_positions,
            alive=alive,
            heartbeat_metric=heartbeat_metric,
            session_metric=session_metric,
            last_switch_metric=last_switch_metric,
            next_switch_metric=next_switch_metric,
            alerts=alerts,
        )

    positions = _structure_positions(selected, actual_positions)

    open_legs = [
        leg for leg in selected.legs if not positions[leg.underlying].is_flat
    ]
    missing_legs = 0 < len(open_legs) < len(selected.legs)
    if missing_legs:
        remaining = "、".join(leg.underlying for leg in open_legs)
        alerts.append(
            PanelAlert(
                key="swap_carry_single_leg",
                level="critical",
                title=f"Swap carry 缺腿裸仓：只剩 {remaining}",
                action="停止新增仓位并立即人工恢复对冲或安全减掉全部剩余腿",
            )
        )

    selected_underlyings = {leg.underlying for leg in selected.legs}
    residual_positions = [
        position
        for position in actual_positions
        if not position.is_flat
        and position.market not in selected_underlyings
        and position.market not in IGNORED_EXTERNAL_POSITIONS
    ]
    if residual_positions:
        residual_names = "、".join(position.market for position in residual_positions)
        alerts.append(
            PanelAlert(
                key="swap_carry_residual_position",
                level="critical",
                title=f"Swap carry 发现结构外残留持仓：{residual_names}",
                action="停止新增仓位并立即人工核对残留腿来源，确认后安全减仓",
            )
        )

    notionals: dict[str, Decimal | None] = {}
    for leg in selected.legs:
        price = (
            carry._metadata_price(metadata, leg)
            if metadata is not None
            else None
        )
        notionals[leg.underlying] = (
            abs(positions[leg.underlying].signed_size) * price
            if price is not None
            else None
        )

    net_delta = carry._positions_net_delta(selected, positions)
    net_tone = "good" if abs(net_delta) <= carry.XAUS_QTY_STEP else "bad"

    liquidation_metrics: list[Metric] = []
    xaus_distance: Decimal | None = None
    for leg in selected.legs:
        metric, distance = await _liquidation_metric(
            client,
            label=f"{leg.underlying} 强平",
            underlying=leg.underlying,
            position=positions[leg.underlying],
        )
        liquidation_metrics.append(metric)
        mode = guard._margin_mode_from_supported_assets(metadata, leg)
        if mode is None:
            # 面板不为识别模式新增 POST 询价，使用守护进程已确认的模式。
            try:
                saved = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                mode_name = saved.get("legs", {}).get(leg.underlying, {}).get("margin_mode")
                mode = guard.MarginModeStatus(str(mode_name), "守护心跳")
            except (OSError, ValueError, AttributeError):
                pass
        if mode is not None and mode.isolated and not positions[leg.underlying].is_flat:
            liquidation_metrics.append(await _allocation_metric(client, leg.underlying))
        if leg.underlying == "XAUS":
            xaus_distance = distance
    if xaus_distance is not None and xaus_distance < guard.LIQUIDATION_ALERT_RATIO:
        alerts.append(
            PanelAlert(
                key="swap_carry_xaus_liquidation",
                level="critical",
                title=f"XAUS 强平距离仅 {xaus_distance:.2%}",
                action="立即人工核对保证金，并按结构顺序安全减小各腿仓位",
            )
        )

    rates: dict[str, Decimal] = {}
    for leg in selected.legs:
        try:
            rates[leg.underlying] = await carry._funding_rate_for_leg(client, leg)
        except Exception:  # noqa: BLE001 单腿失败只令净 carry 无数据
            continue
    net_carry = (
        carry._weighted_net_carry(selected, rates)
        if len(rates) == len(selected.legs)
        else None
    )
    if net_carry is not None and net_carry < 0:
        alerts.append(
            PanelAlert(
                key="swap_carry_negative_carry",
                level="warning",
                title=f"Swap carry 已转负至 {net_carry:.1%}/年",
                action="停止加仓并人工复核各腿资金费；确认持续为负后择机平掉结构",
            )
        )

    try:
        settled = await carry._settled_funding_by_leg(
            client,
            since=_week_start(observed_at),
            structure=selected,
        )
        funding_metric = Metric(
            "本周已结算资金费",
            _funding_value(settled, selected),
        )
    except Exception:  # noqa: BLE001 资金费失败不遮蔽仓位与风险
        funding_metric = Metric("本周已结算资金费", "无数据")

    if net_carry is None:
        carry_value = "无数据"
    else:
        carry_value = f"{net_carry:+.1%}"

    current_name = "空仓" if all(position.is_flat for position in positions.values()) else selected.name
    metrics = [Metric("当前结构", current_name), *_selection_metrics(heartbeat_path, current_name)]
    metrics.extend(
        Metric(
            f"{leg.underlying} {'多腿' if leg.open_side is carry.Side.BUY else '空腿'}",
            _leg_value(
                positions[leg.underlying].signed_size,
                notionals[leg.underlying],
                leg.weight,
                _position_upnl(positions[leg.underlying]),
            ),
        )
        for leg in selected.legs
    )
    metrics.extend([
        Metric("净 delta", f"{net_delta:+.5f}", net_tone),
        Metric("净 carry 年化", carry_value, _carry_tone(net_carry)),
        *liquidation_metrics,
    ])
    if selected.has_xaus:
        metrics.append(_schedule_metric(schedule))
    metrics.extend([
        session_metric,
        heartbeat_metric,
        funding_metric,
        last_switch_metric,
        next_switch_metric,
    ])
    metrics.extend(
        _actual_position_metrics(
            [
                position
                for position in actual_positions
                if position.market not in selected_underlyings
            ]
        )
        if any(
            position.market not in selected_underlyings
            for position in actual_positions
        )
        else []
    )

    if missing_legs:
        summary = "缺腿持仓，需立即处理"
    elif not open_legs:
        if selected.has_xaus:
            summary = (
                "空仓，可交易"
                if schedule is not None and schedule.is_tradable
                else "空仓，等待开市"
            )
        else:
            summary = "空仓，24/7 可交易"
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


def _cost_metrics(path: Path, reconciliation_path: Path | None, *, equity_path: Path | None = None):
    """只读成本区块独立降级；默认按最近连续权益段对账。"""
    rows, error = cost.load(path)
    if error:
        return [Metric("成本明细", error, "warn"), Metric("未解释残差", "不可用", "warn"),
                Metric("成本证据完整性", "证据不完整，不能确认闭合", "warn"),
                Metric("磨损事件", "台账不可用，无法展示逐笔记录", "warn")], []
    totals = cost.summarize(rows)
    metrics = [Metric("成本明细 " + label, f"{totals[kind]:+.2f} USD", _carry_tone(totals[kind]))
               for kind, label in cost.LABELS.items()]
    metrics.extend([Metric("成本合计", f"{totals['total']:+.2f} USD", _carry_tone(totals['total'])),
                    Metric("成本汇总范围", "全台账累计，仅 XAUS/XAU/XAUT；切换父项不重复计入合计")])
    events = sorted((r for r in rows if r['event_type'] in cost.LABELS and
                     r['market'] in cost.MARKETS | {'XAUS/XAU/XAUT'}),
                    key=lambda r: cost.timestamp(r['ts']), reverse=True)[:10]
    for index, row in enumerate(events, 1):
        label = cost.LABELS[row['event_type']]
        if row['event_type'] == 'switch':
            label = '切换（不重复计入合计）'
        amount = cost.number(row['amount_usd'])
        metrics.append(Metric(f"磨损事件 {index}",
            f"{row['ts']} / {label} / {row['market']} / {amount:+.2f} USD / {row['source']}",
            _carry_tone(amount)))
    if not events:
        metrics.append(Metric("磨损事件", "暂无磨损记录"))
    if reconciliation_path is not None:
        report, error = cost.read_report(rows, reconciliation_path)
    else:
        report, error = cost.snapshot_report(rows, equity_path or Path(path).with_name('portfolio_equity.jsonl'),
                                             latest_continuous=True)
    if error:
        metrics.append(Metric("未解释残差", error, "warn"))
        metrics.append(Metric("成本证据完整性", "证据不完整，不能确认闭合", "warn"))
        return metrics, []
    metrics.extend([
        Metric("成本口径", report["basis_note"]),
        Metric("成本对账范围", "账户权益对账" if report['scope'] == 'account' else "策略对账（含账户公共科目）"),
        Metric("成本对账区间", f"{report['start_ts']} → {report['end_ts']}"),
        Metric("成本证据完整性", "证据完整" if report['evidence_complete'] else
               "证据不完整，不能确认闭合（未实现浮动变化不可用）",
               "normal" if report['evidence_complete'] else "warn"),
    ])
    for label, key in [('期初权益', 'start_equity'), ('期末权益', 'end_equity'),
                       ('权益变化', 'equity_change'), ('已解释合计', 'explained'),
                       ('区间成本合计', 'total'), ('其它策略（含 BTC）', 'other_strategies'),
                       ('平台盈亏滑点抵销', 'spread_embedded_adjustment'), ('未解释残差', 'residual')]:
        value = report[key]
        tone = 'bad' if key == 'residual' and report['warning'] else _carry_tone(value)
        metrics.append(Metric(label, f"{value:+.2f} USD", tone))
    ratio = report['residual_ratio']
    metrics.append(Metric("残差占比", f"{ratio:.2f}%" if ratio is not None else "不适用（权益变化为零）",
                          'bad' if report['warning'] else 'normal'))
    metrics.append(Metric("成本残差阈值", f"{report['threshold']:.2f} USD"))
    alerts = [PanelAlert(key="swap_carry_cost_residual", level="warning",
                         title=f"Swap carry 未解释残差 {report['residual']:+.2f} USD 超过阈值",
                         action="核对流水覆盖、成交滑点、其它策略及权益快照口径")
              ] if report['warning'] else []
    return metrics, alerts


def collect(
    *,
    client: Any | None = None,
    heartbeat_path: Path = carry.SWAP_CARRY_GUARD_HEARTBEAT,
    state_path: Path = carry.SWAP_CARRY_GUARD_STATE,
    switch_history_path: Path = guard.DEFAULT_SWITCH_HISTORY,
    now: datetime | None = None,
    cost_ledger_path: Path = cost.DEFAULT_PATH,
    cost_reconciliation_path: Path | None = None,
    portfolio_equity_path: Path | None = None,
) -> SystemStatus:
    """同步 provider 入口；任何整轮失败都返回 error 卡片。"""
    observed_at = now or datetime.now(timezone.utc)
    owned_client = client is None
    cost_metrics, cost_alerts = _cost_metrics(
        cost_ledger_path, cost_reconciliation_path, equity_path=portfolio_equity_path)

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
                switch_history_path=switch_history_path,
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
        status = asyncio.run(run())
        metric, alert = _rehearsal_metric(heartbeat_path)
        status.metrics.extend(cost_metrics)
        status.alerts.extend(cost_alerts)
        status.metrics.append(metric)
        if alert:
            status.alerts.append(alert)
        return status
    except Exception as exc:  # noqa: BLE001 单个 provider 不能拖垮整页
        session_metric, session_alert = _session_expiry_metric(heartbeat_path)
        rehearsal_metric, rehearsal_alert = _rehearsal_metric(heartbeat_path)
        alerts = [
            alert
            for alert in (
                session_alert,
                rehearsal_alert,
                _guard_alert(state_path),
                _switch_incident_alert(heartbeat_path, state_path),
            )
            if alert is not None
        ]
        return SystemStatus(
            name=NAME,
            alive=None,
            summary="采集失败",
            metrics=[
                *cost_metrics,
                session_metric,
                rehearsal_metric,
                _last_switch_metric(switch_history_path),
                _next_switch_metric(None),
            ],
            error=str(exc),
            alerts=[*alerts, *cost_alerts],
        )
