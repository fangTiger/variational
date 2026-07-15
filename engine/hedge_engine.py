"""跨所对冲主循环。

策略：primary 腿（Variational）持有仓位赚 OI 积分，hedge 腿（Extended）持有
等额反向仓位对冲方向风险。引擎监控两腿净 delta，超阈值就把 hedge 腿调整到
-primary_size，使组合净敞口回到 ~0。

安全默认：dry_run=True，只计算与记录意图，不真正下单。确认字段与账户后再关闭。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from adapters.base import ExchangeAdapter, Position
from engine.risk import RiskAction, RiskManager
from infra.logger import get_logger

logger = get_logger("hedge_engine")


@dataclass
class HedgeConfig:
    """对冲引擎配置。"""

    market: str = "BTC-USD"                     # hedge 腿标的名（Extended 口径）
    primary_market: str = "BTC-PERP"            # primary 腿标的名（Variational 口径）
    poll_interval: float = 15.0                 # 轮询间隔（秒）
    # 再平衡阈值：净 delta 占单腿名义比例，超过才动手（减少交易磨损）
    rebalance_threshold_ratio: Decimal = Decimal("0.02")
    dry_run: bool = True                        # 只算不下单


@dataclass
class HedgeState:
    """一轮循环的状态快照。"""

    primary: Position | None = None
    hedge: Position | None = None
    net_delta: Decimal = Decimal(0)
    action_taken: str = ""


class HedgeEngine:
    """对冲引擎：primary 持仓 + hedge 对冲。"""

    def __init__(
        self,
        primary: ExchangeAdapter,
        hedge: ExchangeAdapter,
        config: HedgeConfig | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.primary = primary
        self.hedge = hedge
        self.config = config or HedgeConfig()
        self.risk = risk or RiskManager()
        self._running = False

    async def connect(self) -> None:
        """连接两腿。"""
        await asyncio.gather(self.primary.connect(), self.hedge.connect())
        logger.info("两腿已连接：primary=%s hedge=%s", self.primary.name, self.hedge.name)

    async def run_once(self) -> HedgeState:
        """执行一轮：读持仓 → 风控 → 必要时再平衡。"""
        state = HedgeState()

        # 1. 读两腿持仓（容错，单腿失败不抛出，交给风控裁决）
        primary_ok = hedge_ok = True
        try:
            state.primary = await self.primary.get_position(self.config.primary_market)
        except Exception as exc:  # noqa: BLE001 单腿故障需被风控接管
            primary_ok = False
            logger.error("读取 primary 持仓失败：%s", exc)
        try:
            state.hedge = await self.hedge.get_position(self.config.market)
        except Exception as exc:  # noqa: BLE001
            hedge_ok = False
            logger.error("读取 hedge 持仓失败：%s", exc)

        p_size = state.primary.signed_size if state.primary else Decimal(0)
        h_size = state.hedge.signed_size if state.hedge else Decimal(0)
        state.net_delta = p_size + h_size

        # 2. 风控裁决
        assessment = self.risk.assess(
            primary_size=p_size,
            hedge_size=h_size,
            primary_ok=primary_ok,
            hedge_ok=hedge_ok,
        )
        logger.info(
            "primary=%s hedge=%s net_delta=%s → 风控:%s(%s)",
            p_size, h_size, state.net_delta, assessment.action.value, assessment.reason,
        )

        if assessment.action is RiskAction.FLATTEN:
            state.action_taken = await self._emergency_flatten()
            return state
        if assessment.action is RiskAction.PAUSE:
            state.action_taken = "暂停再平衡"
            return state

        # 3. 再平衡：把 hedge 腿调整到 -primary_size
        target_hedge = -p_size
        base = max(abs(p_size), abs(h_size))
        threshold = self.config.rebalance_threshold_ratio * base if base > 0 else Decimal(0)
        if abs(target_hedge - h_size) > threshold:
            state.action_taken = await self._rebalance(target_hedge)
        else:
            state.action_taken = "无需再平衡"
        return state

    async def _rebalance(self, target_hedge: Decimal) -> str:
        """把 hedge 腿调到目标有符号数量。"""
        msg = f"再平衡 hedge → {target_hedge}"
        if self.config.dry_run:
            logger.info("[dry_run] %s（未真正下单）", msg)
            return f"[dry_run] {msg}"
        result = await self.hedge.hedge(self.config.market, target_hedge)
        logger.info("%s 完成：%s", msg, result)
        return msg

    async def _emergency_flatten(self) -> str:
        """紧急平掉两腿，回到无敞口。"""
        msg = "紧急平仓两腿"
        if self.config.dry_run:
            logger.warning("[dry_run] %s（未真正下单）", msg)
            return f"[dry_run] {msg}"
        logger.warning("执行 %s", msg)
        results = await asyncio.gather(
            self.hedge.close_position(self.config.market),
            self.primary.close_position(self.config.primary_market),
            return_exceptions=True,
        )
        logger.warning("平仓结果：%s", results)
        return msg

    async def run_forever(self) -> None:
        """持续运行主循环，直到 stop()。"""
        self._running = True
        await self.connect()
        logger.info("对冲引擎启动（dry_run=%s）", self.config.dry_run)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 循环不因单轮异常退出
                logger.exception("本轮循环异常：%s", exc)
            await asyncio.sleep(self.config.poll_interval)

    def stop(self) -> None:
        """请求停止主循环。"""
        self._running = False
