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
from grid.grid_state import GridState, load_state, save_state
from grid.regime import GridMode, adx, decide_mode, donchian_prev
from grid.risk import hard_stop_triggered
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
    adx_off: float = 999.0               # 默认禁用 ADX 熔断（只靠库存上限控风险）
    adx_resume: float = 999.0            # 迟滞：OFF 后 ADX 须回落到此值以下才恢复
    donchian_period: int = 48
    candle_lookback: int = 200
    poll_interval: float = 30.0
    dry_run: bool = True
    trend_aware: bool = False
    hard_stop_dist: float = 0.12
    state_path: str = "data/grid_state.json"


class GridEngine:
    def __init__(self, ext, config: GridConfig | None = None, fng_provider=None) -> None:
        self.ext = ext
        self.config = config or GridConfig()
        self._fng_provider = fng_provider
        self._orders: dict[int, dict] = {}   # level -> {"id":..., "side": Side}
        self._mode = GridMode.NEUTRAL        # 上一轮 regime（迟滞用）
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
                           fng=fng, adx_off=self.config.adx_off,
                           adx_resume=self.config.adx_resume, prev_mode=self._mode)
        self._mode = mode

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

    async def _order_statuses(self) -> dict:
        """订单 id → 历史订单对象（查终态用）。失败返回空 dict（按未知处理）。"""
        try:
            orders = await self.ext.get_orders_history(self.config.market, limit=100)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询订单历史失败：%s", exc)
            return {}
        return {getattr(o, "id", None): o for o in orders}

    async def _handle_fills(self, inv_usd: float, blocked_side: str | None = None) -> None:
        """处理从盘口消失的跟踪单——按交易所终态区分，只有真成交才翻单。

        订单会因 EXPIRED(有效期到)/CANCELLED(急停或手动)消失，误当成交翻单
        会挂出穿越盘口的反向单（2026-07-20 实盘事故）。
        - FILLED 或有成交量：买成交→上一格挂卖；卖成交→下一格挂买。
        - EXPIRED/CANCELLED：原格原方向重挂。
        - REJECTED/未知：仅移除记录，交给补格逻辑按当前中心重铺。
        - blocked_side：跳过即将挂出的同方向新单。
        """
        if self.config.dry_run:
            return
        open_ids = await self._open_ids()
        missing = {lv: rec for lv, rec in self._orders.items() if rec["id"] not in open_ids}
        if not missing:
            return
        statuses = await self._order_statuses()
        for lv, rec in missing.items():
            self._orders.pop(lv, None)
            o = statuses.get(rec["id"])
            status = str(getattr(o, "status", "") or "") if o else ""
            filled = Decimal(str(getattr(o, "filled_qty", 0) or 0)) if o else Decimal("0")
            if status == "FILLED" or filled > 0:
                if rec["side"] is Side.BUY:      # 买成交 → 上一格挂卖止盈
                    next_level, next_side = lv + 1, Side.SELL
                    fill_why = f"{lv}买成交→挂卖"
                else:                             # 卖成交 → 下一格挂买
                    next_level, next_side = lv - 1, Side.BUY
                    fill_why = f"{lv}卖成交→挂买"
                if next_side.value == blocked_side:
                    continue
                qty = filled if filled > 0 else None
                await self._place(next_level, next_side, inv_usd, why=fill_why, qty=qty)
            elif status in ("EXPIRED", "CANCELLED"):
                if rec["side"].value == blocked_side:
                    continue
                await self._place(lv, rec["side"], inv_usd, why=f"格{lv}{status}重挂")
            else:
                logger.warning("格%d 订单 %s 终态=%s，仅移除不翻单", lv, rec["id"], status or "未知")

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
        """严格库存上限：真实持仓与该侧挂单全部成交后仍不超上限。"""
        unit = self.config.unit_usd
        inventory_usd = inv_usd if side is Side.BUY else -inv_usd
        pending_usd = sum(unit for rec in self._orders.values() if rec["side"] is side)
        return inventory_usd + pending_usd + unit <= self.config.max_inventory_usd

    async def _place(
        self,
        level: int,
        side: Side,
        inv_usd: float,
        why: str = "",
        qty: Decimal | None = None,
    ) -> str | None:
        if not self._within_cap(side, inv_usd):
            return None  # 超库存上限，不挂
        lp = self._level_price(level)
        qty = qty if qty is not None else Decimal(str(self.config.unit_usd)) / lp
        msg = f"{why}：{side.value} {qty:.6f}@{lp:.0f}(格{level})"
        if self.config.dry_run:
            logger.info("[dry_run] %s", msg)
            self._orders[level] = {"id": f"dry-{level}", "side": side}
            return msg
        try:
            res = await self.ext.place_limit_order(self.config.market, side, qty, lp)
            data = getattr(res, "data", None)
            oid = getattr(data, "id", None) or getattr(res, "id", None)
            if str(getattr(data, "status", "") or "") == "REJECTED":
                logger.warning("%s → 被拒(%s)，不入账", msg, getattr(data, "status_reason", None))
                return None
            self._orders[level] = {"id": oid, "side": side}
            logger.info("%s → id=%s", msg, oid)
            return msg
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂单失败 %s：%s", msg, exc)
            return None

    async def _cancel(self, level: int, why: str = "") -> None:
        rec = self._orders.get(level)
        if not rec:
            return
        if self.config.dry_run:
            logger.info("[dry_run] %s：撤 格%d", why, level)
            self._orders.pop(level, None)
            return
        try:
            await self.ext.cancel_order(rec["id"])
            self._orders.pop(level, None)  # 撤单成功后才删记录
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤单失败 格%d：%s（保留记录下轮重试）", level, exc)

    async def _check_hard_stop(self) -> bool:
        """检查距强平价距离；空仓与有仓但缺失清算信息必须区别处理。"""
        pos = await self.ext.get_position(self.config.market)
        signed_size = pos.signed_size
        if signed_size == 0:
            return False

        liquidation_info = await self.ext.get_liquidation_info(self.config.market)
        if liquidation_info is None:
            logger.error(
                "硬止损检查进入 fail-safe：当前有仓位 %s，但无法取得清算价",
                signed_size,
            )
            return False

        mark, liq = liquidation_info
        if not hard_stop_triggered(
            float(mark),
            float(liq),
            float(signed_size),
            self.config.hard_stop_dist,
        ):
            return False

        logger.critical(
            "触发硬止损：mark=%s liq=%s 持仓=%s 阈值=%.2f%%",
            mark,
            liq,
            signed_size,
            self.config.hard_stop_dist * 100,
        )
        await self._go_off_confirmed(signed_size)
        return True

    async def _go_off_confirmed(self, inv: Decimal) -> bool:
        """持久化急停并反复减仓，连续两次确认空仓后才撤 TPSL。"""
        current_state = load_state(self.config.state_path)
        if current_state is None:
            halted_state = GridState(
                band_low=0.0,
                band_high=0.0,
                frozen=True,
                blocked_side=None,
                halted=True,
            )
        else:
            halted_state = GridState(
                band_low=current_state.band_low,
                band_high=current_state.band_high,
                frozen=current_state.frozen,
                blocked_side=current_state.blocked_side,
                halted=True,
            )
        save_state(self.config.state_path, halted_state)

        logger.critical("确认平仓链启动：初始持仓=%s，已持久化 halted=true", inv)
        try:
            await self.ext.cancel_grid_orders(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.exception("只撤网格单失败，继续执行确认平仓链：%s", exc)

        max_close_attempts = 3
        close_attempts = 0
        consecutive_flat_reads = 0
        while True:
            pos = await self.ext.get_position(self.config.market)
            remaining = pos.signed_size
            if remaining == 0:
                consecutive_flat_reads += 1
                if consecutive_flat_reads < 2:
                    continue
                await self.ext.cancel_tpsl(self.config.market)
                logger.critical("已连续两次确认空仓，撤除 TPSL，确认平仓链完成")
                return True

            consecutive_flat_reads = 0
            if close_attempts >= max_close_attempts:
                logger.error(
                    "确认平仓失败：%d 次 reduce_only 下单后仍有仓位 %s；保留 TPSL",
                    close_attempts,
                    remaining,
                )
                return False

            side = Side.SELL if remaining > 0 else Side.BUY
            close_attempts += 1
            try:
                await self.ext.market_order(
                    self.config.market,
                    side,
                    abs(remaining),
                    reduce_only=True,
                )
                logger.warning(
                    "确认平仓第 %d/%d 次：%s %s（reduce_only）",
                    close_attempts,
                    max_close_attempts,
                    side.value,
                    abs(remaining),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "确认平仓第 %d/%d 次下单失败：%s",
                    close_attempts,
                    max_close_attempts,
                    exc,
                )

    async def _go_off(self, inv: Decimal) -> str:
        # 撤所有挂单
        if self._orders:
            logger.warning("急停：撤 %d 个挂单", len(self._orders))
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
