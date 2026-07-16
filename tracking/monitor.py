"""积分 + 资金费实时监控（阶段 A）。

采集两腿的资金费与账户积分，计算净 carry 与推荐对冲方向，并把快照记入
MetricsTracker 供趋势分析。入金前也能空跑（只监控积分与资金费）。

⚠️ 资金费单位假设（需用一次真实资金费结算校准）：
- Variational /funding/v2 的 predicted_funding_rate 视为「百分比 / funding_interval_s」，
  BTC 间隔 28800s(8h)。即 0.062 表示 0.062% / 8h。
- Extended market_statistics 的 funding_rate 视为「小数 / 1 小时」，
  即 0.000013 表示 0.0013% / 小时。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from infra.logger import get_logger
from tracking.metrics import MetricsTracker, Snapshot

if TYPE_CHECKING:  # 仅类型检查导入，避免纯逻辑测试被 x10 依赖拖累
    from adapters.extended_client import ExtendedClient
    from adapters.variational_client import VariationalClient

logger = get_logger("monitor")

# 归一化基准：每 8 小时、每年（一年 = 3 段 8h × 365）
_PER_YEAR_FROM_8H = Decimal(3 * 365)


@dataclass
class FundingView:
    """两腿资金费对比与方向建议（均已归一化到 % / 8h）。"""

    var_pct_8h: Decimal          # Variational 每 8h 费率（%）
    ext_pct_8h: Decimal          # Extended 每 8h 费率（%）
    # 方案一：Variational 做空 + Extended 做多 的净 carry（% / 8h）
    carry_short_var_pct_8h: Decimal
    recommended: str             # 推荐方向说明
    annualized_pct: Decimal      # 推荐方向的年化 carry（%）

    def pretty(self) -> str:
        return (
            f"资金费(%/8h)  Variational={self.var_pct_8h:+.4f}  Extended={self.ext_pct_8h:+.4f}\n"
            f"  推荐方向：{self.recommended}\n"
            f"  净 carry：{self.carry_short_var_pct_8h:+.4f}%/8h（年化 {self.annualized_pct:+.1f}%）"
        )


def compute_funding_view(
    var_rate_raw: Decimal,
    ext_rate_raw: Decimal,
    *,
    var_interval_s: int = 28800,
    ext_interval_s: int = 3600,
) -> FundingView:
    """把两腿原始费率归一化到 %/8h 并计算净 carry。

    var_rate_raw: Variational 值（百分比 / var_interval）。
    ext_rate_raw: Extended 值（小数 / ext_interval）。
    """
    # Variational：已是百分比 / 间隔 → 换算到 8h
    var_pct_8h = var_rate_raw * (Decimal(28800) / Decimal(var_interval_s))
    # Extended：小数 / 间隔 → ×100 变百分比，再换算到 8h
    ext_pct_8h = ext_rate_raw * 100 * (Decimal(28800) / Decimal(ext_interval_s))

    # 方案一：Variational 做空(收 var)、Extended 做多(付 ext) → 净 = var - ext
    carry_short_var = var_pct_8h - ext_pct_8h
    # 方案二反向 → 净 = ext - var（= -carry_short_var）
    if carry_short_var >= 0:
        recommended = "Variational 做空 + Extended 做多（收 Variational 资金费）"
        best = carry_short_var
    else:
        recommended = "Variational 做多 + Extended 做空（收 Extended 资金费）"
        best = -carry_short_var

    return FundingView(
        var_pct_8h=var_pct_8h,
        ext_pct_8h=ext_pct_8h,
        carry_short_var_pct_8h=carry_short_var,
        recommended=recommended,
        annualized_pct=best * _PER_YEAR_FROM_8H,
    )


@dataclass
class MonitorSnapshot:
    """一次监控采集的结果。"""

    total_points: Decimal
    rank: int
    week_points: Decimal
    next_drop_ts: str
    funding: FundingView
    var_notional_usd: Decimal
    ext_signed_size: Decimal

    def pretty(self) -> str:
        return (
            f"积分：{self.total_points}（本周 +{self.week_points}，排名 {self.rank}）"
            f" 下次结算 {self.next_drop_ts}\n"
            f"{self.funding.pretty()}\n"
            f"持仓：Variational 名义 ${self.var_notional_usd:.0f} | Extended {self.ext_signed_size}"
        )


async def gather(
    var: VariationalClient,
    ext: ExtendedClient,
    *,
    underlying: str = "BTC",
    ext_market: str = "BTC-USD",
) -> MonitorSnapshot:
    """采集两腿积分与资金费，组装监控快照。"""
    # 积分
    summary = await var.get_points_summary()
    total_points = Decimal(str(summary["total_points"]))
    rank = int(summary.get("rank", 0))

    # 本周积分（points/history 最后一个窗口）
    week_points = Decimal(0)
    try:
        history = await var.raw("/points/history")
        if isinstance(history, list) and history:
            week_points = Decimal(str(history[-1].get("self_points", "0")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 points/history 失败：%s", exc)

    next_drop = "?"
    try:
        nd = await var.raw("/points/next_drop_ts")
        next_drop = nd.get("next_drop_ts", "?")
    except Exception:  # noqa: BLE001
        pass

    # 资金费
    var_rate = await var.get_funding_rate(underlying)
    ext_stats = await ext._client.info.get_market_statistics(market_name=ext_market)
    ext_rate = Decimal(str(ext_stats.data.funding_rate))
    funding = compute_funding_view(var_rate, ext_rate)

    # 持仓（入金前均为 0）
    var_pos = await var.get_position(underlying)
    ext_pos = await ext.get_position(ext_market)
    mark = Decimal(str(ext_stats.data.mark_price))
    var_notional = abs(var_pos.signed_size) * mark

    return MonitorSnapshot(
        total_points=total_points,
        rank=rank,
        week_points=week_points,
        next_drop_ts=next_drop,
        funding=funding,
        var_notional_usd=var_notional,
        ext_signed_size=ext_pos.signed_size,
    )


async def run_once(
    var: VariationalClient, ext: ExtendedClient, tracker: MetricsTracker
) -> MonitorSnapshot:
    """采集一次、打印、并记入 tracker。"""
    snap = await gather(var, ext)
    logger.info("\n%s", snap.pretty())
    tracker.record(
        Snapshot(
            ts=time.time(),
            notional_usd=float(snap.var_notional_usd),
            points_total=float(snap.total_points),
            net_funding_usd=0.0,  # 实际收付资金费待有持仓后从成交/账户流水统计
            wear_usd=0.0,
        )
    )
    return snap
