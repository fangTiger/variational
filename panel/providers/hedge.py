"""Lighter RH 积分对冲 provider。

只读机器人写出的心跳快照，不查交易所——面板每 5 秒刷新一次，
每次都去打两个交易所的接口既慢又可能触发限频。
代价是权益数据拿不到，所以这张卡的 equity 为 None、不计入总览。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from panel.types import Metric, SystemStatus

_ROOT = Path(__file__).resolve().parent.parent.parent
_SNAPSHOT = _ROOT / "data" / "lighter_hedge.jsonl"
_BASELINE = _ROOT / "data" / "hedge_baseline.json"

NAME = "RH 积分对冲"
_DEFAULT_INTERVAL = 30.0


def _last_jsonl(path: Path) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(row, dict):
            return row
    return None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 缺失或损坏都当作没有
        return None
    return data if isinstance(data, dict) else None


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _date(value) -> str | None:
    """把有效 Unix 秒格式化为面板使用的月日。"""
    timestamp = _dec(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _tone(value: Decimal | None) -> str:
    """收益正绿负红，零或缺失使用普通色。"""
    if value is None or value == 0:
        return "normal"
    return "good" if value > 0 else "bad"


def _leg_text(size: Decimal | None, notional: Decimal | None) -> str:
    """单腿展示：有多少显示多少。

    缺名义金额只是少一项信息，不会误导，所以仍要把数量显示出来——
    机器人重启前 notional 字段不存在，若整行降级为「—」，
    反而把本来就有的持仓数量也弄丢了。

    注意这与「总收益」「两腿浮盈」不同：那两个是合计值，
    只有半边数据算出来的结果是错的，必须整项降级。
    """
    if size is None:
        return "—"
    if notional is None:
        return f"{size:+.5f} BTC"
    return f"{size:+.5f} BTC / ${abs(notional):,.0f}"


def _position_pnl_tone(value: Decimal | None) -> str:
    """按两腿合计浮盈的绝对偏离判断对冲是否正常。"""
    if value is None:
        return "normal"
    absolute = abs(value)
    if absolute < Decimal("5"):
        return "good"
    if absolute < Decimal("20"):
        return "warn"
    return "bad"


def collect(
    *,
    snapshot_path: Path = _SNAPSHOT,
    baseline_path: Path = _BASELINE,
    now: float | None = None,
) -> SystemStatus:
    """产出对冲卡片。读不到心跳一律按失联处理，不抛异常。"""
    now = time.time() if now is None else now
    row = _last_jsonl(snapshot_path)
    if row is None:
        return SystemStatus(
            name=NAME,
            alive=False,
            summary="读不到心跳",
            error="data/lighter_hedge.jsonl 缺失或损坏",
        )
    baseline = _read_json(baseline_path) or {}

    ts = row.get("ts")
    interval = row.get("interval") or _DEFAULT_INTERVAL
    try:
        age = now - float(ts)
    except (TypeError, ValueError):
        age = float("inf")
    alive = age <= 3 * float(interval)

    net = _dec(row.get("net_delta"))
    if net is None:
        net_text, net_tone = "读数异常", "bad"
    elif net == 0:
        net_text, net_tone = "0 ✅", "good"
    else:
        net_text, net_tone = f"{net:+} ⚠️ 裸敞口", "bad"

    margin = _dec(row.get("hedge_free_margin_ratio"))
    min_margin = _dec(row.get("min_hedge_free_margin_ratio"))
    if margin is None:
        margin_text, margin_tone = "—", "normal"
    else:
        margin_text = f"{margin:.1%}"
        margin_tone = "bad" if (min_margin is not None and margin < min_margin) else "good"

    # 两条腿是一个整体，缺任何一条就不汇总——少算一半比不显示更误导
    collateral = _dec(row.get("primary_collateral"))
    hedge_equity = _dec(row.get("hedge_equity"))
    equity_decimal = (
        collateral + hedge_equity
        if collateral is not None and hedge_equity is not None
        else None
    )
    equity = float(equity_decimal) if equity_decimal is not None else None

    baseline_total = _dec(baseline.get("total"))
    baseline_date = _date(baseline.get("ts"))
    if (
        equity_decimal is not None
        and baseline_total is not None
        and baseline_date is not None
    ):
        total_pnl_decimal = equity_decimal - baseline_total
        total_pnl = float(total_pnl_decimal)
        total_pnl_text = f"${total_pnl_decimal:+,.2f}（自 {baseline_date}）"
    else:
        total_pnl_decimal = None
        total_pnl = None
        total_pnl_text = "—"

    primary_size = _dec(row.get("primary_size"))
    hedge_size = _dec(row.get("hedge_size"))
    primary_notional = _dec(row.get("primary_notional"))
    hedge_notional = _dec(row.get("hedge_notional"))

    capped = bool(row.get("primary_notional_exceeded"))
    metrics = [
        Metric("Lighter 腿", _leg_text(primary_size, primary_notional)),
        Metric("Extended 腿", _leg_text(hedge_size, hedge_notional)),
        Metric("净敞口", net_text, net_tone),
        Metric("对冲腿保证金", margin_text, margin_tone),
        Metric("名义上限", "已触发，拒绝跟单" if capped else "未触发", "bad" if capped else "good"),
        Metric("心跳", f"{age:.0f} 秒前" if age != float("inf") else "—",
               "good" if alive else "bad"),
    ]

    max_primary_notional = _dec(row.get("max_primary_notional"))
    notional_text = (
        f"${primary_notional:,.0f}" if primary_notional is not None else "—"
    )
    if (
        primary_notional is None
        or max_primary_notional is None
        or max_primary_notional <= 0
    ):
        usage_text, usage_tone = "—", "normal"
    else:
        usage = abs(primary_notional) / max_primary_notional
        usage_text = (
            f"${primary_notional:,.0f} / ${max_primary_notional:,.0f} "
            f"({usage:.0%})"
        )
        if abs(primary_notional) > max_primary_notional:
            usage_tone = "bad"
        elif usage > Decimal("0.90"):
            usage_tone = "warn"
        else:
            usage_tone = "normal"

    collateral_text = f"${collateral:,.2f}" if collateral is not None else "—"
    hedge_equity_text = (
        f"${hedge_equity:,.2f}" if hedge_equity is not None else "—"
    )
    rebalance_threshold = _dec(row.get("rebalance_threshold_ratio"))
    rebalance_text = (
        f"{rebalance_threshold:.1%}" if rebalance_threshold is not None else "—"
    )
    primary_unrealized = _dec(row.get("primary_unrealized"))
    hedge_unrealized = _dec(row.get("hedge_unrealized"))
    if primary_unrealized is None or hedge_unrealized is None:
        position_pnl = None
        position_pnl_text = "—"
    else:
        position_pnl = primary_unrealized + hedge_unrealized
        position_pnl_text = f"${position_pnl:+,.2f}"
    metrics.extend(
        [
            Metric("名义金额", notional_text),
            Metric("上限占用", usage_text, usage_tone),
            Metric("Lighter 权益", collateral_text),
            Metric("Extended 权益", hedge_equity_text),
            Metric("总收益", total_pnl_text, _tone(total_pnl_decimal)),
            Metric(
                "当前持仓浮盈",
                position_pnl_text,
                _position_pnl_tone(position_pnl),
            ),
            Metric("再平衡阈值", rebalance_text),
        ]
    )
    return SystemStatus(
        name=NAME,
        alive=alive,
        summary=str(row.get("action", "—")),
        metrics=metrics,
        equity=equity,
        total_pnl=total_pnl,
    )
