"""Lighter RH 积分对冲 provider。

只读机器人写出的心跳快照，不查交易所——面板每 5 秒刷新一次，
每次都去打两个交易所的接口既慢又可能触发限频。
代价是权益数据拿不到，所以这张卡的 equity 为 None、不计入总览。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from panel.types import Metric, SystemStatus

_ROOT = Path(__file__).resolve().parent.parent.parent
_SNAPSHOT = _ROOT / "data" / "lighter_hedge.jsonl"

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


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def collect(*, snapshot_path: Path = _SNAPSHOT, now: float | None = None) -> SystemStatus:
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
    equity = (
        float(collateral + hedge_equity)
        if collateral is not None and hedge_equity is not None
        else None
    )

    capped = bool(row.get("primary_notional_exceeded"))
    metrics = [
        Metric("Lighter 腿", str(row.get("primary_size", "—"))),
        Metric("Extended 腿", str(row.get("hedge_size", "—"))),
        Metric("净敞口", net_text, net_tone),
        Metric("对冲腿保证金", margin_text, margin_tone),
        Metric("名义上限", "已触发，拒绝跟单" if capped else "未触发", "bad" if capped else "good"),
        Metric("心跳", f"{age:.0f} 秒前" if age != float("inf") else "—",
               "good" if alive else "bad"),
    ]
    return SystemStatus(
        name=NAME,
        alive=alive,
        summary=str(row.get("action", "—")),
        metrics=metrics,
        equity=equity,
    )
