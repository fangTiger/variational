"""实盘网格引擎（Extended，BTC）。

复用回测验证过的逻辑：几何格子、逐格成交、regime 急停、库存上限。
执行用市价单（回测显示费率影响很小），dry_run 默认只算不下单。

安全：live 启动时若账户已有该标的持仓（非本引擎所开，可能是 farm 的对冲腿），
拒绝启动——防止与 farm 撞车。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from adapters.base import Side
from grid.regime import GridMode, adx, decide_mode, donchian_prev
from infra.logger import get_logger

logger = get_logger("grid_engine")


@dataclass
class GridConfig:
    market: str = "BTC-USD"
    spacing_pct: float = 0.02            # 格距（回测最优区间 1.5-2.5%）
    unit_usd: float = 50.0               # 每格名义
    max_inventory_usd: float = 400.0     # 最大库存名义（封趋势亏损）
    adx_period: int = 14
    adx_off: float = 30.0                # ADX 超此值急停
    donchian_period: int = 48            # 通道突破周期（小时）
    candle_lookback: int = 200           # 每轮取多少根小时K线算指标
    poll_interval: float = 60.0
    dry_run: bool = True


class GridEngine:
    def __init__(self, ext, config: GridConfig | None = None, fng_provider=None) -> None:
        self.ext = ext
        self.config = config or GridConfig()
        self._fng_provider = fng_provider  # 可选：返回当前恐惧贪婪值的可调用
        self._last_level: int | None = None
        self._running = False

    @property
    def _log_step(self) -> float:
        return math.log(1 + self.config.spacing_pct)

    async def connect(self) -> None:
        await self.ext.connect()
        # live 安全检查：账户不能已有该标的持仓（防与 farm 撞车）
        pos = await self.ext.get_position(self.config.market)
        if not self.config.dry_run and pos.signed_size != 0:
            raise RuntimeError(
                f"账户已有 {self.config.market} 持仓 {pos.signed_size}（可能是 farm 对冲腿）。"
                f"网格需从空仓起步，请先平掉再启动，避免撞车。"
            )
        logger.info("网格引擎连接完成（dry_run=%s，起始持仓=%s）", self.config.dry_run, pos.signed_size)

    async def _indicators(self):
        """取近端K线，算 ADX 与 Donchian 通道，返回最新值 + 现价。"""
        r = await self.ext._client.info.get_candles_history(
            market_name=self.config.market, candle_type="trades",
            interval="PT1H", limit=self.config.candle_lookback,
        )
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        highs = [float(k.high) for k in candles]
        lows = [float(k.low) for k in candles]
        closes = [float(k.close) for k in candles]
        a = adx(highs, lows, closes, self.config.adx_period)
        up, lo = donchian_prev(highs, lows, self.config.donchian_period)
        return closes[-1], a[-1], up[-1], lo[-1]

    async def run_once(self) -> str:
        price, adx_val, dc_up, dc_lo = await self._indicators()
        fng = self._fng_provider() if self._fng_provider else None
        mode = decide_mode(
            adx_val=adx_val, close=price, donchian_up=dc_up, donchian_lo=dc_lo,
            fng=fng, adx_off=self.config.adx_off,
        )

        pos = await self.ext.get_position(self.config.market)
        inv = pos.signed_size          # BTC，本引擎的净库存
        inv_usd = abs(float(inv) * price)
        level = round(math.log(price) / self._log_step)
        if self._last_level is None:
            self._last_level = level

        logger.info("price=%.0f ADX=%s mode=%s inv=%s(≈$%.0f) level=%d",
                    price, f"{adx_val:.1f}" if adx_val else None, mode.value, inv, inv_usd, level)

        if mode is GridMode.OFF:
            self._last_level = level
            if inv != 0:
                side = Side.SELL if inv > 0 else Side.BUY
                return await self._trade(side, abs(inv), reduce_only=True, why="急停平库存")
            return "OFF（无库存）"

        # NEUTRAL：逐格成交
        actions = []
        if level < self._last_level:            # 跌 → 买
            for lv in range(self._last_level - 1, level - 1, -1):
                if inv_usd >= self.config.max_inventory_usd and inv > 0:
                    actions.append("库存已达上限，停止买入")
                    break
                lp = math.exp(lv * self._log_step)
                q = Decimal(str(self.config.unit_usd / lp))
                actions.append(await self._trade(Side.BUY, q, why=f"跌破{lv}格买"))
        elif level > self._last_level:          # 涨 → 卖
            for lv in range(self._last_level + 1, level + 1):
                if inv_usd >= self.config.max_inventory_usd and inv < 0:
                    actions.append("库存已达下限，停止卖出")
                    break
                lp = math.exp(lv * self._log_step)
                q = Decimal(str(self.config.unit_usd / lp))
                actions.append(await self._trade(Side.SELL, q, why=f"涨过{lv}格卖"))
        self._last_level = level
        return "；".join(actions) if actions else "无格子触发"

    async def _trade(self, side: Side, qty: Decimal, *, reduce_only: bool = False, why: str = "") -> str:
        msg = f"{why}：{side.value} {qty:.6f} BTC"
        if self.config.dry_run:
            logger.info("[dry_run] %s", msg)
            return f"[dry_run] {msg}"
        r = await self.ext.market_order(self.config.market, side, qty, reduce_only=reduce_only)
        logger.info("%s → %s", msg, str(r)[:120])
        return msg

    async def run_forever(self) -> None:
        import asyncio

        self._running = True
        await self.connect()
        logger.info("网格引擎启动（dry_run=%s，格距%.1f%%，库存上限$%.0f）",
                    self.config.dry_run, self.config.spacing_pct * 100, self.config.max_inventory_usd)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("本轮异常：%s", exc)
            await asyncio.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False
