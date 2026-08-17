"""收益归因 provider。

用户没有安装每小时的归因服务，所以这里现算。
pnl_attribution.run() 会打一次资金费接口，因此必须缓存——
面板每 5 秒刷新一次，不缓存会把接口打爆。
"""

from __future__ import annotations

import time

from panel.types import Metric, SystemStatus

NAME = "收益归因"
CACHE_TTL_S = 300.0

_cache: tuple[float, dict] | None = None


def reset_cache() -> None:
    """清空缓存。测试用。"""
    global _cache
    _cache = None


def _run() -> dict:
    """真正跑一次归因。抽成独立函数便于测试替换。"""
    from tools.pnl_attribution import run

    return run()


def collect(*, now: float | None = None) -> SystemStatus:
    """产出归因卡片。算不出来就降级成 error 卡片，不影响其他卡片。"""
    global _cache
    now = time.time() if now is None else now

    if _cache is not None and now - _cache[0] < CACHE_TTL_S:
        result = _cache[1]
    else:
        try:
            result = _run()
        except Exception as exc:  # noqa: BLE001 归因失败不能拖垮整页
            return SystemStatus(
                name=NAME,
                alive=None,
                summary="归因计算失败",
                error=str(exc),
            )
        _cache = (now, result)

    residual = result.get("residual")
    metrics = [
        Metric("闭环数", str(result.get("loops_count", "—"))),
        Metric("闭环利润", f"${result.get('grid_pnl', 0):+.2f}", "good"),
        Metric("方向性盈亏", f"${result.get('directional_pnl', 0):+.2f}"),
        Metric(
            "残差",
            f"${residual:+.2f}" if isinstance(residual, (int, float)) else "—",
            "bad" if result.get("has_gap") else "good",
        ),
        Metric("观察期", f"{result.get('days', 0):.1f} / 28 天"),
    ]
    return SystemStatus(
        name=NAME,
        alive=None,
        summary=str(result.get("reason", "—")),
        metrics=metrics,
        equity=None,
    )
