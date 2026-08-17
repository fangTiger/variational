"""BTC 网格 provider：读两个本地快照文件，产出一张卡片。"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from panel.types import Metric, SystemStatus

_ROOT = Path(__file__).resolve().parent.parent.parent
_LIVE = _ROOT / "data" / "grid_live.json"
_MONITOR = _ROOT / "data" / "grid_monitor.jsonl"
_EQUITY_PEAK = _ROOT / "data" / "equity_peak.json"
_BASELINE = _ROOT / "data" / "grid_baseline.json"
_ATTRIBUTION = _ROOT / "data" / "attribution.json"

NAME = "BTC 网格"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 缺失或损坏都当作没有
        return None
    return data if isinstance(data, dict) else None


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


def _number(value) -> float | None:
    """把快照字段安全转成有限浮点数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value) -> str | None:
    """把有效 Unix 秒格式化为面板使用的月日。"""
    timestamp = _number(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp).strftime("%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _tone(value: float | None) -> str:
    """收益正绿负红，零或缺失使用普通色。"""
    if value is None or value == 0:
        return "normal"
    return "good" if value > 0 else "bad"


def collect(
    *,
    live_path: Path = _LIVE,
    monitor_path: Path = _MONITOR,
    equity_peak_path: Path = _EQUITY_PEAK,
    baseline_path: Path = _BASELINE,
    attribution_path: Path = _ATTRIBUTION,
) -> SystemStatus:
    """产出网格卡片。任何读取失败都降级为 error 卡片，不抛异常。"""
    live = _read_json(live_path)
    monitor = _last_jsonl(monitor_path) or {}
    equity_peak = _read_json(equity_peak_path) or {}
    baseline = _read_json(baseline_path) or {}
    attribution = _read_json(attribution_path) or {}

    if live is None:
        return SystemStatus(
            name=NAME,
            alive=False,
            summary="读不到引擎快照",
            error="data/grid_live.json 缺失或损坏",
        )

    cfg = live.get("cfg") or {}
    halted = bool(live.get("halted"))
    frozen = bool(live.get("frozen"))
    if halted:
        guard_value, guard_tone = "已熔断停机", "bad"
    elif frozen:
        guard_value, guard_tone = "冻结中", "warn"
    else:
        guard_value, guard_tone = "正常", "good"

    inv_btc = _number(live.get("inv_btc"))
    inv_usd = _number(live.get("inv_usd"))
    max_inv = _number(cfg.get("max_inv"))

    # 浮盈直接并进持仓行——用户要的是"这个仓位现在赚亏多少"，
    # 和持仓数量放一起看才有意义，单独一行容易被忽略
    current_pnl = _number(attribution.get("unrealised_end"))
    current_pnl_tone = _tone(current_pnl)

    if inv_btc is None:
        position_text = "—"
    else:
        position_text = f"{inv_btc:+.5f} BTC"
        if inv_usd is not None:
            position_text += f" / ${inv_usd:,.0f}"
        if current_pnl is not None:
            position_text += f"（浮盈 ${current_pnl:+,.2f}）"
    inv_text = "—"
    inv_tone = "normal"
    if inv_usd is not None and max_inv is not None and max_inv != 0:
        absolute_inv = abs(inv_usd)
        absolute_max = abs(max_inv)
        usage = absolute_inv / absolute_max
        inv_text = f"${absolute_inv:,.0f} / ${absolute_max:,.0f} ({usage:.0%})"
        inv_tone = "warn" if usage > 0.8 else "normal"

    equity = _number(monitor.get("equity"))
    metrics = [
        Metric("权益", f"${equity:,.2f}" if equity is not None else "—"),
        Metric("持仓", position_text),
        Metric("库存上限", inv_text, inv_tone),
        Metric("模式", str(live.get("mode", "—"))),
        Metric("熔断", guard_value, guard_tone),
    ]

    baseline_equity = _number(baseline.get("equity"))
    baseline_date = _date(baseline.get("ts"))
    if baseline_equity is not None and baseline_date is not None:
        total_pnl = equity - baseline_equity if equity is not None else None
        total_pnl_text = (
            f"${total_pnl:+,.2f}（自 {baseline_date}）"
            if total_pnl is not None
            else f"—（自 {baseline_date}）"
        )
    else:
        total_pnl = _number(monitor.get("pnl_since_start"))
        total_pnl_text = (
            f"${total_pnl:+,.2f}（基线未知）"
            if total_pnl is not None
            else "—（基线未知）"
        )
    total_pnl_tone = _tone(total_pnl)

    current_pnl_text = (
        f"${current_pnl:+,.2f}" if current_pnl is not None else "—"
    )

    peak = _number(equity_peak.get("peak"))
    if equity is None or equity <= 0 or peak is None or peak <= 0:
        breaker_text, breaker_tone = "—", "normal"
    else:
        breaker_line = peak * (1 - 0.12)
        breaker_distance = equity - breaker_line
        breaker_ratio = round(breaker_distance / equity, 12)
        breaker_text = f"${breaker_distance:,.0f} ({breaker_ratio:.1%})"
        if breaker_ratio < 0.05:
            breaker_tone = "bad"
        elif breaker_ratio < 0.10:
            breaker_tone = "warn"
        else:
            breaker_tone = "good"

    liquidation_distance = _number(live.get("dist_to_liq_pct"))
    if liquidation_distance is None:
        liquidation_text, liquidation_tone = "—", "normal"
    else:
        liquidation_text = f"{liquidation_distance:.1%}"
        if liquidation_distance < 0.15:
            liquidation_tone = "bad"
        elif liquidation_distance < 0.25:
            liquidation_tone = "warn"
        else:
            liquidation_tone = "good"

    band_low = _number(live.get("band_low"))
    band_high = _number(live.get("band_high"))
    mark = _number(live.get("mark"))
    if band_low is None or band_high is None:
        band_text = "—"
    else:
        band_text = f"${band_low:,.0f} ~ ${band_high:,.0f}"

    if (
        mark is None
        or band_low is None
        or band_high is None
        or band_high <= band_low
    ):
        position_text, position_tone = "—", "normal"
    else:
        band_position = round((mark - band_low) / (band_high - band_low), 12)
        position_text = f"{band_position:.0%}"
        position_tone = (
            "warn" if band_position < 0.10 or band_position > 0.90 else "normal"
        )

    adx = _number(live.get("adx"))
    adx_text = f"{adx:.1f}" if adx is not None else "—"

    spacing = _number(cfg.get("spacing"))
    unit = _number(cfg.get("unit"))
    spacing_text = (
        f"{spacing:.4%} / ${unit:,.0f}"
        if spacing is not None and unit is not None
        else "—"
    )

    replacement_placed = _number(live.get("replacement_placed"))
    fill_detected = _number(live.get("fill_detected"))
    replacement_failed = _number(live.get("replacement_failed"))
    order_success_text = (
        f"{replacement_placed:,.0f} / {fill_detected:,.0f}"
        if replacement_placed is not None and fill_detected is not None
        else "—"
    )
    order_success_tone = (
        "warn" if replacement_failed is not None and replacement_failed > 0 else "normal"
    )

    max_abs_inv_usd = _number(live.get("max_abs_inv_usd"))
    max_inv_number = _number(max_inv)
    max_inventory_text = (
        f"${max_abs_inv_usd:,.0f}" if max_abs_inv_usd is not None else "—"
    )
    max_inventory_tone = (
        "warn"
        if (
            max_abs_inv_usd is not None
            and max_inv_number is not None
            and max_abs_inv_usd > max_inv_number
        )
        else "normal"
    )

    metrics.extend(
        [
            Metric("总收益", total_pnl_text, total_pnl_tone),
            Metric("当前持仓浮盈", current_pnl_text, current_pnl_tone),
            Metric("距熔断线", breaker_text, breaker_tone),
            Metric("距强平", liquidation_text, liquidation_tone),
            Metric("band 区间", band_text),
            Metric("价格位置", position_text, position_tone),
            Metric("ADX", adx_text),
            Metric("格距 / 每格", spacing_text),
            Metric("下单成功", order_success_text, order_success_tone),
            Metric("历史最大库存", max_inventory_text, max_inventory_tone),
        ]
    )
    return SystemStatus(
        name=NAME,
        alive=not halted,
        summary=f"标记价 ${live.get('mark', 0):,.0f}",
        metrics=metrics,
        equity=equity,
        total_pnl=total_pnl,
    )
