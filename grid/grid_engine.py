"""实盘限价单网格引擎（Extended，BTC）。

真正的网格：在盘口挂一排限价单——当前价下方挂买单、上方挂卖单（几何等距）。
成交即翻单：买单成交→在上一格挂卖单止盈；卖单成交→在下一格挂买单。maker 费。
regime 急停：强趋势/突破 → 撤所有单 + 市价平库存。库存上限封趋势亏损。

安全：专用账户；重启自动接管账户已有持仓与挂单（对账后续挂）。
dry_run 只打印意图不真正挂单。
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
    spacing_pct: float = 0.02            # 格距
    unit_usd: float = 50.0               # 每格名义
    max_inventory_usd: float = 150.0     # 最大库存名义
    levels_per_side: int = 4             # 上下各挂几格
    adx_period: int = 14
    adx_off: float = 30.0
    donchian_period: int = 48
    candle_lookback: int = 200
    poll_interval: float = 30.0
    dry_run: bool = True


class GridEngine:
    def __init__(self, ext, config: GridConfig | None = None, fng_provider=None) -> None:
        self.ext = ext
        self.config = config or GridConfig()
        self._fng_provider = fng_provider
        self._orders: dict[int, dict] = {}   # level -> {"id":..., "side": Side}
        self._running = False

    @property
    def _log_step(self) -> float:
        return math.log(1 + self.config.spacing_pct)

    def _level_price(self, level: int) -> Decimal:
        return Decimal(str(math.exp(level * self._log_step)))

    def _price_level(self, price: float) -> int:
        return round(math.log(price) / self._log_step)

    async def connect(self) -> None:
        await self.ext.connect()
        pos = await self.ext.get_position(self.config.market)
        if not self.config.dry_run and pos.signed_size != 0:
            stats = await self.ext._client.info.get_market_statistics(market_name=self.config.market)
            notional = abs(float(pos.signed_size)) * float(stats.data.mark_price)
            if notional > 2 * self.config.max_inventory_usd:
                raise RuntimeError(
                    f"账户已有持仓 {pos.signed_size}(≈${notional:.0f})远超库存上限，疑似外部仓，请先平掉。")
            logger.warning("接管账户已有持仓 %s 作为网格库存", pos.signed_size)
        # 接管账户上已存在的本网格挂单（重启对账）
        await self._adopt_open_orders()
        logger.info("网格引擎连接完成（dry_run=%s，持仓=%s，接管挂单=%d）",
                    self.config.dry_run, pos.signed_size, len(self._orders))

    async def _adopt_open_orders(self) -> None:
        """把交易所上已有的挂单按格价归入本引擎状态（重启接管）。"""
        try:
            orders = await self.ext.get_open_orders(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询挂单失败：%s", exc)
            return
        for o in orders:
            price = float(getattr(o, "price", 0) or 0)
            if price <= 0:
                continue
            lv = self._price_level(price)
            side = Side.BUY if "BUY" in str(getattr(o, "side", "")).upper() else Side.SELL
            self._orders[lv] = {"id": getattr(o, "id", None), "side": side}

    async def _inv(self) -> tuple[Decimal, float]:
        pos = await self.ext.get_position(self.config.market)
        stats = await self.ext._client.info.get_market_statistics(market_name=self.config.market)
        price = float(stats.data.mark_price)
        return pos.signed_size, price

    async def run_once(self) -> str:
        # 指标 + regime
        r = await self.ext._client.info.get_candles_history(
            market_name=self.config.market, candle_type="trades",
            interval="PT1H", limit=self.config.candle_lookback)
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        highs = [float(k.high) for k in candles]; lows = [float(k.low) for k in candles]
        closes = [float(k.close) for k in candles]
        a = adx(highs, lows, closes, self.config.adx_period)
        up, lo = donchian_prev(highs, lows, self.config.donchian_period)
        price = closes[-1]
        fng = self._fng_provider() if self._fng_provider else None
        mode = decide_mode(adx_val=a[-1], close=price, donchian_up=up[-1], donchian_lo=lo[-1],
                           fng=fng, adx_off=self.config.adx_off)

        inv, mark = await self._inv()
        inv_usd = float(inv) * mark
        logger.info("price=%.0f ADX=%s mode=%s inv=%s(≈$%.0f) 挂单=%d",
                    price, f"{a[-1]:.1f}" if a[-1] else None, mode.value, inv, inv_usd, len(self._orders))

        if mode is GridMode.OFF:
            return await self._go_off(inv)

        # 1) 处理成交：已挂但盘口消失的单=成交，翻到反向一格
        await self._handle_fills(inv_usd)
        # 2) 维护阶梯：当前价上下各挂 N 格
        return await self._maintain_ladder(price, inv_usd)

    async def _open_ids(self) -> set:
        orders = await self.ext.get_open_orders(self.config.market)
        return {getattr(o, "id", None) for o in orders}

    async def _handle_fills(self, inv_usd: float) -> None:
        """跟踪单从盘口消失=成交：买成交→上一格挂卖；卖成交→下一格挂买。"""
        if self.config.dry_run:
            return
        open_ids = await self._open_ids()
        for lv in list(self._orders.keys()):
            rec = self._orders[lv]
            if rec["id"] in open_ids:
                continue
            # 成交
            self._orders.pop(lv, None)
            if rec["side"] is Side.BUY:      # 买成交 → 上一格挂卖止盈
                await self._place(lv + 1, Side.SELL, inv_usd, why=f"{lv}买成交→挂卖")
            else:                             # 卖成交 → 下一格挂买
                await self._place(lv - 1, Side.BUY, inv_usd, why=f"{lv}卖成交→挂买")

    async def _maintain_ladder(self, price: float, inv_usd: float) -> str:
        center = self._price_level(price)
        n = self.config.levels_per_side
        acted = []
        # 下方挂买（_place 内部按库存上限守卫）
        for lv in range(center - 1, center - n - 1, -1):
            if lv not in self._orders:
                m = await self._place(lv, Side.BUY, inv_usd, why="补买格")
                if m:
                    acted.append(m)
        # 上方挂卖
        for lv in range(center + 1, center + n + 1):
            if lv not in self._orders:
                m = await self._place(lv, Side.SELL, inv_usd, why="补卖格")
                if m:
                    acted.append(m)
        # 撤掉远离区间的陈单
        for lv in list(self._orders.keys()):
            if lv < center - n - 2 or lv > center + n + 2:
                await self._cancel(lv, why="撤陈单")
        return "；".join(acted) if acted else "阶梯已就位"

    def _within_cap(self, side: Side, inv_usd: float) -> bool:
        """严格库存上限：现有挂单全部成交后，长/短库存名义仍不超上限。"""
        unit = self.config.unit_usd
        maxinv = self.config.max_inventory_usd
        n_buy = sum(1 for r in self._orders.values() if r["side"] is Side.BUY)
        n_sell = sum(1 for r in self._orders.values() if r["side"] is Side.SELL)
        if side is Side.BUY:
            return inv_usd + (n_buy + 1) * unit <= maxinv
        return -inv_usd + (n_sell + 1) * unit <= maxinv

    async def _place(self, level: int, side: Side, inv_usd: float, why: str = "") -> str | None:
        if not self._within_cap(side, inv_usd):
            return None  # 超库存上限，不挂
        lp = self._level_price(level)
        qty = Decimal(str(self.config.unit_usd)) / lp
        msg = f"{why}：{side.value} {qty:.6f}@{lp:.0f}(格{level})"
        if self.config.dry_run:
            logger.info("[dry_run] %s", msg)
            self._orders[level] = {"id": f"dry-{level}", "side": side}
            return msg
        try:
            res = await self.ext.place_limit_order(self.config.market, side, qty, lp)
            oid = getattr(getattr(res, "data", None), "id", None) or getattr(res, "id", None)
            self._orders[level] = {"id": oid, "side": side}
            logger.info("%s → id=%s", msg, oid)
            return msg
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂单失败 %s：%s", msg, exc)
            return None

    async def _cancel(self, level: int, why: str = "") -> None:
        rec = self._orders.pop(level, None)
        if not rec:
            return
        if self.config.dry_run:
            logger.info("[dry_run] %s：撤 格%d", why, level)
            return
        try:
            await self.ext.cancel_order(rec["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤单失败 格%d：%s", level, exc)

    async def _go_off(self, inv: Decimal) -> str:
        # 撤所有挂单
        for lv in list(self._orders.keys()):
            await self._cancel(lv, why="急停撤单")
        # 平库存
        if inv != 0:
            side = Side.SELL if inv > 0 else Side.BUY
            if self.config.dry_run:
                logger.warning("[dry_run] 急停平库存 %s %s", side.value, abs(inv))
            else:
                await self.ext.market_order(self.config.market, side, abs(inv), reduce_only=True)
                logger.warning("急停已平库存 %s %s", side.value, abs(inv))
        return "OFF：已撤单+平库存"

    async def run_forever(self) -> None:
        import asyncio

        self._running = True
        await self.connect()
        logger.info("网格引擎启动（dry_run=%s，格距%.1f%%，每边%d格，库存上限$%.0f）",
                    self.config.dry_run, self.config.spacing_pct * 100,
                    self.config.levels_per_side, self.config.max_inventory_usd)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("本轮异常：%s", exc)
            await asyncio.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False
