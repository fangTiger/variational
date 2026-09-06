"""积分 + 资金费实时监控（阶段 A）。

采集两腿的资金费与账户积分，计算净 carry 与推荐对冲方向，并把快照记入
MetricsTracker 供趋势分析。入金前也能空跑（只监控积分与资金费）。

⚠️ 资金费单位口径：
- Variational /funding/v2 的 predicted_funding_rate 是年化小数（×100 为年化百分比），
  单期小数费率 = 年化值 ÷ (365 天 / funding_interval_s)。依据是 /transfers 的
  已结算单期 funding_rate 能精确解释扣款，且按 365 天折算可还原 6 位小数年化值；
  8h 与 4h 市场的费率帽也都折算为同一个 0.109500 年化值。
- Extended market_statistics 的 funding_rate 暂按「小数 / 1 小时」处理，
  即 0.000013 表示 0.0013% / 小时；该口径尚未经真实结算校准。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from infra.logger import get_logger
from tracking.direction_state import FundingDirectionStateStore
from tracking.metrics import MetricsTracker, Snapshot

if TYPE_CHECKING:  # 仅类型检查导入，避免纯逻辑测试被 x10 依赖拖累
    from adapters.extended_client import ExtendedClient
    from adapters.variational_client import VariationalClient

logger = get_logger("monitor")

# 归一化基准：每 8 小时、每年（固定采用 365 天基准）
_SECONDS_PER_YEAR = Decimal(365 * 24 * 60 * 60)
_PER_YEAR_FROM_8H = Decimal(3 * 365)
_UNSTABLE_RELATIVE_GAP = Decimal("0.10")

@dataclass
class FundingView:
    """两腿预测资金费对比与方向建议（费率均折算为每 8 小时百分比）。"""

    var_pct_8h: Decimal          # Variational 每 8h 费率（%）
    ext_pct_8h: Decimal          # Extended 每 8h 费率（%）
    # 方案一：Variational 做空 + Extended 做多 的净 carry（% / 8h）
    carry_short_var_pct_8h: Decimal
    direction: str               # 机器可读方向，供调用方显式保存
    recommended: str             # 推荐方向说明
    annualized_pct: Decimal      # 推荐方向的年化 carry（%）
    extended_calibrated: bool    # Extended 原始费率单位是否已经结算记录校准
    warnings: tuple[str, ...]    # 方向翻转或判定不稳健等显式告警

    def pretty(self) -> str:
        lines = [
            f"预测资金费（折算 %/8h）  Variational={self.var_pct_8h:+.4f}  "
            f"Extended={self.ext_pct_8h:+.4f}",
            "  ⚠️ Extended 资金费单位未经校准（缺少真实结算记录），折算值与推荐方向不可全信。",
            f"  推荐方向：{self.recommended}",
            (
                f"  净 carry：{self.carry_short_var_pct_8h:+.4f}%/8h"
                f"（年化 {self.annualized_pct:+.1f}%）"
            ),
        ]
        lines.extend(f"  ⚠️ {warning}" for warning in self.warnings)
        return "\n".join(lines)


def _funding_rates_are_close(var_pct_8h: Decimal, ext_pct_8h: Decimal) -> bool:
    """判断两腿折算费率是否接近到不足以稳健决定方向。"""
    difference = abs(var_pct_8h - ext_pct_8h)
    scale = max(abs(var_pct_8h), abs(ext_pct_8h))
    if scale == 0:
        return True
    return difference / scale <= _UNSTABLE_RELATIVE_GAP


def compute_funding_view(
    var_rate_raw: Decimal,
    ext_rate_raw: Decimal,
    *,
    var_interval_s: int = 28800,
    ext_interval_s: int = 3600,
    previous_direction: str | None = None,
) -> FundingView:
    """把两腿原始费率归一化到 %/8h 并计算净 carry。

    var_rate_raw: Variational 年化小数；单期费率按 365 天基准换算。
    ext_rate_raw: Extended 每 ``ext_interval_s`` 的小数费率；该口径未经结算校准。
    """
    if var_interval_s <= 0:
        raise ValueError("Variational 资金费周期必须为正数")
    if ext_interval_s <= 0:
        raise ValueError("Extended 资金费周期必须为正数")

    # Variational：年化小数 → 当前单期百分比 → 统一折算到 8h。
    # 两步中的 var_interval_s 会约掉；保留展开写法以明确单期费率定义。
    periods_per_year = _SECONDS_PER_YEAR / Decimal(var_interval_s)
    var_period_pct = var_rate_raw / periods_per_year * 100
    var_pct_8h = var_period_pct * (Decimal(28800) / Decimal(var_interval_s))
    # Extended：暂按小数 / 间隔 → ×100 变百分比，再换算到 8h；未经真实结算校准。
    ext_pct_8h = ext_rate_raw * 100 * (Decimal(28800) / Decimal(ext_interval_s))

    # 方案一：Variational 做空(收 var)、Extended 做多(付 ext) → 净 = var - ext
    carry_short_var = var_pct_8h - ext_pct_8h
    # 方案二反向 → 净 = ext - var（= -carry_short_var）
    if carry_short_var >= 0:
        direction = "short_variational"
        recommended = "Variational 做空 + Extended 做多（收 Variational 资金费）"
        best = carry_short_var
    else:
        direction = "long_variational"
        recommended = "Variational 做多 + Extended 做空（收 Extended 资金费）"
        best = -carry_short_var

    warnings: list[str] = []
    if previous_direction is not None and direction != previous_direction:
        warning = (
            "与上次运行相比方向翻转：本次推荐方向与上一次资金费观测相反；"
            "Extended 单位未经校准，请勿据此自动换向。"
        )
        warnings.append(warning)
        logger.warning(warning)

    if _funding_rates_are_close(var_pct_8h, ext_pct_8h):
        warning = (
            "方向判定不稳健：两腿折算费率差不超过较大绝对费率的 10%；"
            "微小波动即可改变推荐方向。"
        )
        warnings.append(warning)
        logger.warning(warning)

    return FundingView(
        var_pct_8h=var_pct_8h,
        ext_pct_8h=ext_pct_8h,
        carry_short_var_pct_8h=carry_short_var,
        direction=direction,
        recommended=recommended,
        annualized_pct=best * _PER_YEAR_FROM_8H,
        extended_calibrated=False,
        warnings=tuple(warnings),
    )


def compute_funding_view_with_state(
    var_rate_raw: Decimal,
    ext_rate_raw: Decimal,
    *,
    venue: str,
    market: str,
    state_path: str | Path | None = None,
    var_interval_s: int = 28800,
    ext_interval_s: int = 3600,
) -> FundingView:
    """读取上次方向、计算本次视图并保存方向。

    状态读写失败由存储层降级处理，不会阻断本次资金费计算。
    """
    store = (
        FundingDirectionStateStore()
        if state_path is None
        else FundingDirectionStateStore(state_path)
    )
    previous_direction = store.load(venue, market)
    view = compute_funding_view(
        var_rate_raw,
        ext_rate_raw,
        var_interval_s=var_interval_s,
        ext_interval_s=ext_interval_s,
        previous_direction=previous_direction,
    )
    store.save(venue, market, view.direction)
    return view


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
    previous_direction: str | None = None,
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
    funding = compute_funding_view(
        var_rate,
        ext_rate,
        previous_direction=previous_direction,
    )

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
    var: VariationalClient,
    ext: ExtendedClient,
    tracker: MetricsTracker,
    *,
    previous_direction: str | None = None,
) -> MonitorSnapshot:
    """采集一次、打印、并记入 tracker。"""
    snap = await gather(var, ext, previous_direction=previous_direction)
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
