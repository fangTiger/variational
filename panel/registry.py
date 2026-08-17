"""系统注册表。加一个新系统就在 PROVIDERS 里加一行，渲染器不用动。"""

from __future__ import annotations

import math
from collections.abc import Callable

from panel.actions import to_panel_alert
from panel.providers import attribution, grid, hedge
from panel.types import PanelAlert, SystemStatus

#: (显示名, 采集函数)。加系统改这里，其他文件都不用动。
PROVIDERS: list[tuple[str, Callable[[], SystemStatus]]] = [
    (grid.NAME, grid.collect),
    (hedge.NAME, hedge.collect),
    (attribution.NAME, attribution.collect),
]


def _collect_alerts():
    """抽成函数便于测试替换。"""
    from tools.alert_check import collect_alerts

    return collect_alerts()


def collect_all() -> list[SystemStatus]:
    """依次采集所有系统。单个失败降级为 error 卡片，绝不向上抛。"""
    result: list[SystemStatus] = []
    for name, fn in PROVIDERS:
        try:
            result.append(fn())
        except Exception as exc:  # noqa: BLE001 一个坏了不能拖垮整页
            result.append(
                SystemStatus(name=name, alive=None, summary="采集失败", error=str(exc))
            )
    return result


def total_equity(systems: list[SystemStatus]) -> float:
    """总览条用的合计权益。equity 为 None 的系统不计入。"""
    return sum(s.equity for s in systems if isinstance(s.equity, (int, float)))


def system_counts(systems: list[SystemStatus]) -> tuple[int, int]:
    """返回有存活概念的系统总数与在线数。"""
    total = sum(system.alive is not None for system in systems)
    online = sum(system.alive is True for system in systems)
    return total, online


def total_pnl_summary(systems: list[SystemStatus]) -> tuple[float, list[str]]:
    """汇总可计算的总收益，并返回未计入的系统名称。"""
    total = 0.0
    missing: list[str] = []
    for system in systems:
        value = system.total_pnl
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            total += float(value)
        else:
            missing.append(system.name)
    return total, missing


def collect_panel_alerts() -> list[PanelAlert]:
    """取全局告警并翻译成带动作指引的形式。"""
    try:
        raw = _collect_alerts()
    except Exception as exc:  # noqa: BLE001
        return [
            PanelAlert(
                key="panel_alert_failure",
                level="critical",
                title="⛔ 告警系统本身取数失败",
                action=f"面板无法判断是否有异常，不要依赖本页。告诉 Claude 排查：{exc}",
            )
        ]
    return [to_panel_alert(a) for a in raw]
