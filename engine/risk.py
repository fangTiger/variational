"""对冲引擎风控。

职责：在每轮再平衡前后做安全检查，异常时给出动作建议（收缩/暂停/紧急平仓）。
本模块只做**决策**，实际下单由引擎执行，便于单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class RiskAction(Enum):
    """风控裁决。"""

    OK = "ok"                  # 正常，可继续
    PAUSE = "pause"            # 暂停再平衡（如点差骤增），但不平仓
    FLATTEN = "flatten"        # 紧急平掉两腿（如单腿故障导致裸敞口）


@dataclass
class RiskConfig:
    """风控参数。"""

    # 最大净敞口容忍：占单腿名义数量的比例，超过视为对冲失效
    max_net_delta_ratio: Decimal = Decimal("0.05")
    # 保证金率安全下限（低于则收缩/告警），各平台口径不同，实盘校准
    min_margin_ratio: Decimal = Decimal("0.15")
    # 单次点差容忍（bps），超过暂停再平衡避免高磨损
    max_spread_bps: Decimal = Decimal("30")


@dataclass
class RiskAssessment:
    """一次风控评估结果。"""

    action: RiskAction
    reason: str


class RiskManager:
    """基于当前状态给出风控裁决。"""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def assess(
        self,
        *,
        primary_size: Decimal,
        hedge_size: Decimal,
        primary_ok: bool,
        hedge_ok: bool,
        spread_bps: Decimal | None = None,
    ) -> RiskAssessment:
        """评估当前对冲状态。

        primary_size / hedge_size: 两腿有符号数量（对冲成功时应互为相反数）。
        primary_ok / hedge_ok: 两腿数据是否成功获取（断连检测）。
        spread_bps: 当前点差（可选）。
        """
        # 单腿断连 → 存在裸敞口风险，紧急平仓另一腿
        if not primary_ok or not hedge_ok:
            return RiskAssessment(
                RiskAction.FLATTEN,
                f"单腿数据异常（primary_ok={primary_ok}, hedge_ok={hedge_ok}），紧急平仓避免裸敞口",
            )

        # 点差骤增 → 暂停再平衡
        if spread_bps is not None and spread_bps > self.config.max_spread_bps:
            return RiskAssessment(
                RiskAction.PAUSE, f"点差 {spread_bps}bps 超过阈值，暂停再平衡"
            )

        # 净敞口过大 → 对冲已明显失衡（引擎会尝试再平衡，这里仅标记）
        net = primary_size + hedge_size
        base = max(abs(primary_size), abs(hedge_size))
        if base > 0 and abs(net) > self.config.max_net_delta_ratio * base:
            return RiskAssessment(
                RiskAction.OK,
                f"净敞口 {net} 偏大（占比 {abs(net) / base:.2%}），需再平衡",
            )

        return RiskAssessment(RiskAction.OK, "对冲状态正常")
