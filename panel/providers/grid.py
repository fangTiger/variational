"""BTC 网格 provider：读两个本地快照文件，产出一张卡片。"""

from __future__ import annotations

import json
from pathlib import Path

from panel.types import Metric, SystemStatus

_ROOT = Path(__file__).resolve().parent.parent.parent
_LIVE = _ROOT / "data" / "grid_live.json"
_MONITOR = _ROOT / "data" / "grid_monitor.jsonl"

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


def collect(*, live_path: Path = _LIVE, monitor_path: Path = _MONITOR) -> SystemStatus:
    """产出网格卡片。任何读取失败都降级为 error 卡片，不抛异常。"""
    live = _read_json(live_path)
    monitor = _last_jsonl(monitor_path) or {}

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

    inv_usd = live.get("inv_usd")
    max_inv = cfg.get("max_inv")
    inv_text = "—"
    inv_tone = "normal"
    if isinstance(inv_usd, (int, float)) and isinstance(max_inv, (int, float)) and max_inv:
        inv_text = f"${inv_usd:,.0f} / ${max_inv:,.0f}"
        inv_tone = "warn" if abs(inv_usd) > max_inv * 0.8 else "normal"

    equity = monitor.get("equity")
    metrics = [
        Metric("权益", f"${equity:,.2f}" if isinstance(equity, (int, float)) else "—"),
        Metric("持仓", f"{live.get('inv_btc', 0):+.5f} BTC"),
        Metric("库存上限", inv_text, inv_tone),
        Metric("模式", str(live.get("mode", "—"))),
        Metric("熔断", guard_value, guard_tone),
    ]
    return SystemStatus(
        name=NAME,
        alive=not halted,
        summary=f"标记价 ${live.get('mark', 0):,.0f}",
        metrics=metrics,
        equity=equity if isinstance(equity, (int, float)) else None,
    )
